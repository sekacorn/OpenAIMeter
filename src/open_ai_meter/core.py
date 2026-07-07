"""Core measurement, accounting, storage, and reporting primitives."""

from __future__ import annotations

import csv
import html
import json
import math
import re
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol, Self

import yaml

VERSION = "0.1.0a3"
SCHEMA_VERSION = "1.0"
MAX_METADATA_DEPTH = 8
MAX_RECORD_BYTES = 256_000
Currency = Literal["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF"]
VALID_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF"}
HOSTING_MODES = {
    "provider_api",
    "self_hosted",
    "local_device",
    "private_cloud",
    "public_cloud",
    "hybrid",
    "unknown",
}
RECORD_TYPES = {
    "model.usage",
    "model.retry",
    "model.fallback",
    "model.cache_hit",
    "model.cache_miss",
    "model.routing_decision",
    "agent.run",
    "agent.step",
    "agent.delegation",
    "agent.retry",
    "agent.escalation",
    "tool.call",
    "tool.retry",
    "tool.failure",
    "tool.cache_hit",
    "retrieval.query",
    "retrieval.result",
    "embedding.usage",
    "vector_search.usage",
    "document_processing.usage",
    "compute.cpu",
    "compute.gpu",
    "compute.memory",
    "storage.usage",
    "network.egress",
    "database.usage",
    "container.usage",
    "outcome.recorded",
    "outcome.corrected",
    "outcome.invalidated",
    "outcome.value_recorded",
    "budget.created",
    "budget.threshold_reached",
    "budget.exceeded",
    "budget.forecast_exceeded",
    "budget.reset",
    "pricing.loaded",
    "pricing.changed",
    "pricing.missing",
    "pricing.stale",
    "optimization.recommendation",
    "model.replacement.saving",
    "cascade.saving",
    "caching.saving",
    "local_inference.saving",
}
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class OpenAIMeterError(ValueError):
    """Base validation or accounting error."""


class Adapter(Protocol):
    """Protocol for optional integrations that produce usage records."""

    def records(self) -> Iterable[dict[str, Any]]:
        """Yield OpenAIMeter-compatible usage records."""


def now_utc() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(tz=UTC)


def parse_time(value: str) -> datetime:
    """Parse RFC 3339-ish timestamps, normalizing ``Z`` to UTC."""

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OpenAIMeterError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise OpenAIMeterError("timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def isoformat(value: datetime) -> str:
    """Serialize a datetime as RFC 3339 UTC."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def decimal_from(value: Any, field: str) -> Decimal:
    """Parse a Decimal from JSON/YAML-safe values."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OpenAIMeterError(f"{field} must be a valid Decimal") from exc
    if not parsed.is_finite():
        raise OpenAIMeterError(f"{field} must be a finite Decimal")
    return parsed


def money(value: Decimal) -> str:
    """Stable six-place money string for reports."""

    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def require_currency(value: Any) -> str:
    """Validate a three-letter supported currency."""

    currency = str(value)
    if currency not in VALID_CURRENCIES:
        raise OpenAIMeterError(f"unsupported currency: {currency}")
    return currency


def metadata_depth(value: Any, depth: int = 0) -> int:
    """Return nested metadata depth."""

    max_depth = depth
    stack: list[tuple[Any, int]] = [(value, depth)]
    while stack:
        current, current_depth = stack.pop()
        max_depth = max(max_depth, current_depth)
        if max_depth > MAX_METADATA_DEPTH:
            return max_depth
        if isinstance(current, dict):
            stack.extend((item, current_depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, current_depth + 1) for item in current)
    return max_depth


def safe_csv_cell(value: Any) -> str:
    """Escape CSV cells that spreadsheet programs may treat as formulas."""

    text = "" if value is None else str(value)
    # Spreadsheet apps may ignore leading whitespace before formula triggers.
    if text.lstrip().startswith(CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


@dataclass(frozen=True)
class UsageRecord:
    """Versioned AI usage and outcome accounting record."""

    data: dict[str, Any]

    @property
    def record_id(self) -> str:
        return str(self.data["record_id"])

    @property
    def currency(self) -> str:
        return str(self.data.get("cost", {}).get("currency", "USD"))

    @property
    def total_cost(self) -> Decimal:
        return decimal_from(self.data.get("cost", {}).get("total_cost", "0"), "cost.total_cost")

    @property
    def success_weight(self) -> Decimal:
        outcome = self.data.get("outcome") or {}
        if outcome.get("correction_status") == "invalidated":
            return Decimal("0")
        if outcome.get("success") is True:
            return decimal_from(outcome.get("quantity", "1"), "outcome.quantity")
        score = outcome.get("score")
        threshold = outcome.get("threshold")
        if score is not None and threshold is not None:
            score_d = decimal_from(score, "outcome.score")
            return (
                Decimal("1")
                if score_d >= decimal_from(threshold, "outcome.threshold")
                else Decimal("0")
            )
        return Decimal("0")

    @property
    def latency_ms(self) -> Decimal:
        return decimal_from(self.data.get("performance", {}).get("latency_ms", "0"), "latency_ms")

    def to_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, separators=(",", ":"), default=str)


def validate_record(raw: dict[str, Any]) -> UsageRecord:
    """Validate and normalize a usage record without changing its economic meaning."""

    encoded_len = len(json.dumps(raw, default=str).encode("utf-8"))
    if encoded_len > MAX_RECORD_BYTES:
        raise OpenAIMeterError("record is too large")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise OpenAIMeterError("schema_version must be 1.0")
    if not raw.get("record_id"):
        raw["record_id"] = str(uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4())
    record_type = str(raw.get("record_type", ""))
    if record_type not in RECORD_TYPES and not re.match(
        r"^[a-z][a-z0-9_]*\.[a-z0-9_.-]+$", record_type
    ):
        raise OpenAIMeterError(f"unsupported record_type: {record_type}")
    start = parse_time(str(raw["start_time"]))
    end = parse_time(str(raw["end_time"]))
    if end < start:
        raise OpenAIMeterError("end_time must not be before start_time")
    model = raw.get("model") or {}
    if not model.get("provider"):
        raise OpenAIMeterError("model.provider is required")
    hosting = str(model.get("hosting", "unknown"))
    if hosting not in HOSTING_MODES:
        raise OpenAIMeterError(f"unsupported hosting mode: {hosting}")
    usage = raw.get("usage") or {}
    known_total = 0
    all_components_present = True
    for key in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cached_output_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "image_input_units",
    ):
        value = usage.get(key)
        if value is None:
            all_components_present = False
            continue
        if not isinstance(value, int) or value < 0:
            raise OpenAIMeterError(f"usage.{key} must be a nonnegative integer")
        known_total += value
    total = usage.get("total_tokens")
    if total is not None:
        if not isinstance(total, int) or total < 0:
            raise OpenAIMeterError("usage.total_tokens must be a nonnegative integer")
        semantics = usage.get("provider_total_semantics")
        if all_components_present and total != known_total and semantics is None:
            raise OpenAIMeterError("usage.total_tokens is inconsistent with supplied components")
    cost = raw.setdefault("cost", {})
    require_currency(cost.get("currency", "USD"))
    for key in ("provider_cost", "infrastructure_cost", "allocated_cost", "total_cost"):
        if key in cost and cost[key] is not None:
            decimal_from(cost[key], f"cost.{key}")
    metadata = raw.get("metadata") or {}
    if metadata_depth(metadata) > MAX_METADATA_DEPTH:
        raise OpenAIMeterError("metadata depth exceeds safety limit")
    return UsageRecord(raw)


@dataclass(frozen=True)
class PricingTable:
    """Versioned local pricing table with deterministic resolution."""

    version: str
    entries: list[dict[str, Any]]

    @classmethod
    def from_file(cls, path: Path) -> Self:
        data = load_yaml(path)
        return cls(version=str(data["version"]), entries=list(data.get("entries", [])))

    def resolve(self, record: UsageRecord) -> tuple[str, dict[str, Any] | None]:
        model = record.data["model"]
        timestamp = parse_time(str(record.data["start_time"]))
        cost_currency = record.currency
        candidates: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry.get("provider") != model.get("provider"):
                continue
            if entry.get("model_key") not in {
                model.get("pricing_key"),
                model.get("response_model"),
                model.get("requested_model"),
            }:
                continue
            if entry.get("region") not in {None, model.get("region")}:
                continue
            if entry.get("currency") != cost_currency:
                continue
            start = parse_time(str(entry["effective_start"]))
            end_raw = entry.get("effective_end")
            end = parse_time(str(end_raw)) if end_raw else None
            if start <= timestamp and (end is None or timestamp < end):
                candidates.append(entry)
        if len(candidates) == 1:
            return "exact", candidates[0]
        if len(candidates) > 1:
            return "ambiguous", None
        fallbacks = [
            entry
            for entry in self.entries
            if entry.get("provider") == model.get("provider")
            and entry.get("model_key") == model.get("requested_model")
            and entry.get("currency") == cost_currency
        ]
        if len(fallbacks) == 1:
            return "fallback", fallbacks[0]
        return "missing", None

    def sources(self) -> list[dict[str, Any]]:
        """Return deterministic pricing-source summaries."""

        summaries: list[dict[str, Any]] = []
        for entry in self.entries:
            summaries.append(
                {
                    "provider": entry.get("provider"),
                    "model_key": entry.get("model_key"),
                    "currency": entry.get("currency"),
                    "region": entry.get("region"),
                    "source_reference": entry.get("source_reference"),
                    "source_type": entry.get("source_type", "unknown"),
                    "verified_date": entry.get("verified_date"),
                    "expires_at": entry.get("expires_at"),
                    "effective_start": entry.get("effective_start"),
                    "effective_end": entry.get("effective_end"),
                }
            )
        return sorted(summaries, key=lambda item: json.dumps(item, sort_keys=True, default=str))


def pricing_source_warnings(
    pricing: PricingTable, as_of: datetime | None = None, stale_after_days: int = 90
) -> list[dict[str, Any]]:
    """Evaluate pricing-source freshness and expiration warnings."""

    check_time = as_of or now_utc()
    stale_after = timedelta(days=stale_after_days)
    warnings: list[dict[str, Any]] = []
    for entry in pricing.entries:
        label = f"{entry.get('provider')}:{entry.get('model_key')}"
        verified = entry.get("verified_date")
        if not verified:
            warnings.append(
                {"entry": label, "severity": "warning", "reason": "missing_verified_date"}
            )
        else:
            verified_time = (
                parse_time(f"{verified}T00:00:00Z")
                if len(str(verified)) == 10
                else parse_time(str(verified))
            )
            if check_time - verified_time > stale_after:
                warnings.append(
                    {
                        "entry": label,
                        "severity": "warning",
                        "reason": "stale_pricing_source",
                        "verified_date": str(verified),
                    }
                )
        expires = entry.get("expires_at") or entry.get("effective_end")
        if expires:
            expires_time = parse_time(str(expires))
            if check_time >= expires_time:
                warnings.append(
                    {
                        "entry": label,
                        "severity": "critical",
                        "reason": "pricing_expired",
                        "expires_at": str(expires),
                    }
                )
            elif expires_time - check_time <= timedelta(days=14):
                warnings.append(
                    {
                        "entry": label,
                        "severity": "warning",
                        "reason": "pricing_expires_soon",
                        "expires_at": str(expires),
                    }
                )
    return warnings


def calculate_provider_cost(record: UsageRecord, pricing: PricingTable) -> dict[str, Any]:
    """Calculate provider API cost from a local versioned pricing table."""

    provider_reported = record.data.get("cost", {}).get("provider_reported_cost")
    if provider_reported is not None:
        return {
            "status": "provider_reported",
            "provider_cost": money(decimal_from(provider_reported, "provider_reported_cost")),
            "resolution": "provider_reported",
            "components": {"provider_reported_cost": str(provider_reported)},
        }
    resolution, entry = pricing.resolve(record)
    if entry is None:
        return {
            "status": "unknown",
            "provider_cost": None,
            "resolution": resolution,
            "components": {},
        }
    usage = record.data.get("usage", {})
    per_million = Decimal("1000000")
    token_rates = {
        "input": ("input_tokens", "input_token_price"),
        "output": ("output_tokens", "output_token_price"),
        "cached_input": ("cached_input_tokens", "cached_input_price"),
        "cached_output": ("cached_output_tokens", "cached_output_price"),
        "reasoning": ("reasoning_tokens", "reasoning_token_price"),
    }
    missing_rates = [
        rate_field
        for usage_field, rate_field in token_rates.values()
        if decimal_from(usage.get(usage_field, 0), usage_field) > 0
        and entry.get(rate_field) is None
    ]
    if missing_rates:
        return {
            "status": "unknown",
            "provider_cost": None,
            "resolution": "missing_rate",
            "pricing_version": pricing.version,
            "missing_rates": missing_rates,
            "components": {},
        }
    components = {
        "input": decimal_from(usage.get("input_tokens", 0), "input_tokens")
        * decimal_from(entry.get("input_token_price", "0"), "input_token_price")
        / per_million,
        "output": decimal_from(usage.get("output_tokens", 0), "output_tokens")
        * decimal_from(entry.get("output_token_price", "0"), "output_token_price")
        / per_million,
        "cached_input": decimal_from(usage.get("cached_input_tokens", 0), "cached_input_tokens")
        * decimal_from(entry.get("cached_input_price", "0"), "cached_input_price")
        / per_million,
        "cached_output": decimal_from(usage.get("cached_output_tokens", 0), "cached_output_tokens")
        * decimal_from(entry.get("cached_output_price", "0"), "cached_output_price")
        / per_million,
        "reasoning": decimal_from(usage.get("reasoning_tokens", 0), "reasoning_tokens")
        * decimal_from(entry.get("reasoning_token_price", "0"), "reasoning_token_price")
        / per_million,
        "per_request": decimal_from(entry.get("per_request_price", "0"), "per_request_price"),
    }
    subtotal = sum(components.values(), Decimal("0"))
    discount = decimal_from(entry.get("batch_discount", "0"), "batch_discount")
    if discount:
        subtotal *= Decimal("1") - (discount / Decimal("100"))
    return {
        "status": "calculated",
        "provider_cost": money(subtotal),
        "resolution": resolution,
        "pricing_version": pricing.version,
        "components": {key: money(value) for key, value in components.items()},
    }


@dataclass(frozen=True)
class InfrastructureProfile:
    """Local inference cost assumptions."""

    data: dict[str, Any]

    @classmethod
    def from_file(cls, path: Path) -> Self:
        return cls(load_yaml(path))


def calculate_local_inference_cost(
    record: UsageRecord, profile: InfrastructureProfile
) -> dict[str, Any]:
    """Estimate local inference cost from explicit supplied infrastructure assumptions."""

    profile_data = profile.data.get("profile", profile.data)
    currency = require_currency(profile_data.get("currency", record.currency))
    if currency != record.currency:
        raise OpenAIMeterError("record and local profile currencies differ")
    perf = record.data.get("performance", {})
    duration_ms = decimal_from(perf.get("latency_ms"), "performance.latency_ms")
    if duration_ms <= 0:
        raise OpenAIMeterError("local inference duration is required")
    hours = duration_ms / Decimal("3600000")
    compute = profile_data.get("compute", {})
    energy = profile_data.get("energy", {})
    allocation = profile_data.get("allocation", {})
    required = ["gpu_hourly_cost", "cpu_hourly_cost", "memory_hourly_cost"]
    if any(compute.get(key) is None for key in required):
        raise OpenAIMeterError("missing local compute assumptions")
    gpu = hours * decimal_from(compute["gpu_hourly_cost"], "gpu_hourly_cost")
    cpu = hours * decimal_from(compute["cpu_hourly_cost"], "cpu_hourly_cost")
    memory = hours * decimal_from(compute["memory_hourly_cost"], "memory_hourly_cost")
    energy_cost = Decimal("0")
    if energy.get("average_watts") is not None and energy.get("electricity_per_kwh") is not None:
        kwh = (decimal_from(energy["average_watts"], "average_watts") / Decimal("1000")) * hours
        energy_cost = kwh * decimal_from(energy["electricity_per_kwh"], "electricity_per_kwh")
    subtotal = gpu + cpu + memory + energy_cost
    overhead = subtotal * (
        decimal_from(allocation.get("overhead_percentage", "0"), "overhead") / Decimal("100")
    )
    total = subtotal + overhead
    return {
        "status": "estimated",
        "currency": currency,
        "total_cost": money(total),
        "components": {
            "gpu": money(gpu),
            "cpu": money(cpu),
            "memory": money(memory),
            "energy": money(energy_cost),
            "overhead": money(overhead),
        },
        "assumptions": profile_data.get("id", "supplied-profile"),
    }


@dataclass(frozen=True)
class AllocationInput:
    """Input for deterministic infrastructure cost allocation."""

    pool_cost: Decimal
    weights: list[Decimal]
    currency: str = "USD"
    method: str = "custom_weight"


def allocate_costs(allocation: AllocationInput) -> list[Decimal]:
    """Allocate a pool across weights while preserving the rounded total."""

    require_currency(allocation.currency)
    if not allocation.weights or any(weight < 0 for weight in allocation.weights):
        raise OpenAIMeterError("allocation weights must be nonnegative")
    denominator = sum(allocation.weights, Decimal("0"))
    if denominator <= 0:
        raise OpenAIMeterError("allocation denominator must be positive")
    cents = Decimal("0.000001")
    raw = [(allocation.pool_cost * weight / denominator) for weight in allocation.weights]
    rounded = [value.quantize(cents, rounding=ROUND_HALF_UP) for value in raw]
    diff = allocation.pool_cost.quantize(cents, rounding=ROUND_HALF_UP) - sum(rounded, Decimal("0"))
    if rounded:
        rounded[-1] += diff
    return rounded


def allocation_weights(records: Iterable[UsageRecord], method: str) -> list[Decimal]:
    """Derive allocation weights from records for common infrastructure methods."""

    items = list(records)
    if method == "equal_share":
        return [Decimal("1") for _ in items]
    if method == "request_count":
        return [Decimal("1") for _ in items]
    if method == "duration":
        return [max(item.latency_ms, Decimal("0")) for item in items]
    if method == "token_count":
        return [
            decimal_from(item.data.get("usage", {}).get("total_tokens", "0"), "usage.total_tokens")
            for item in items
        ]
    if method == "successful_outcome":
        return [item.success_weight for item in items]
    if method == "workflow_weight":
        return [
            decimal_from(
                item.data.get("attribution", {}).get("workflow_weight", "1"), "workflow_weight"
            )
            for item in items
        ]
    raise OpenAIMeterError(f"unsupported allocation method: {method}")


def allocate_records(
    pool_cost: Decimal, records: Iterable[UsageRecord], method: str, currency: str = "USD"
) -> list[dict[str, Any]]:
    """Allocate a cost pool to records with a named method."""

    items = list(records)
    weights = allocation_weights(items, method)
    amounts = allocate_costs(AllocationInput(pool_cost, weights, currency=currency, method=method))
    denominator = sum(weights, Decimal("0"))
    return [
        {
            "record_id": item.record_id,
            "currency": currency,
            "method": method,
            "weight": str(weight),
            "denominator": str(denominator),
            "allocated_amount": money(amount),
        }
        for item, weight, amount in zip(items, weights, amounts, strict=True)
    ]


@dataclass(frozen=True)
class CostPerSuccessResult:
    """Cost per successful outcome result with completeness status."""

    numerator: Decimal
    denominator: Decimal
    status: str
    currency: str

    @property
    def value(self) -> Decimal | None:
        if self.status != "defined":
            return None
        return self.numerator / self.denominator


def cost_per_success(records: Iterable[UsageRecord]) -> CostPerSuccessResult:
    """Calculate total included cost divided by successful outcomes."""

    items = list(records)
    if not items:
        return CostPerSuccessResult(Decimal("0"), Decimal("0"), "zero_attempts", "USD")
    currencies = {item.currency for item in items}
    if len(currencies) != 1:
        raise OpenAIMeterError("cannot combine currencies")
    numerator = sum((item.total_cost for item in items), Decimal("0"))
    denominator = sum((item.success_weight for item in items), Decimal("0"))
    if denominator == 0:
        return CostPerSuccessResult(numerator, denominator, "zero_successes", currencies.pop())
    return CostPerSuccessResult(numerator, denominator, "defined", currencies.pop())


@dataclass(frozen=True)
class Budget:
    """Budget definition."""

    id: str
    amount: Decimal
    currency: str
    warning_threshold: Decimal
    critical_threshold: Decimal

    @classmethod
    def from_file(cls, path: Path) -> Self:
        data = load_yaml(path)
        return cls(
            id=str(data["id"]),
            amount=decimal_from(data["amount"], "budget.amount"),
            currency=require_currency(data["currency"]),
            warning_threshold=decimal_from(
                data.get("warning_threshold", "80"), "warning_threshold"
            ),
            critical_threshold=decimal_from(
                data.get("critical_threshold", "100"), "critical_threshold"
            ),
        )


def evaluate_budget(records: Iterable[UsageRecord], budget: Budget) -> dict[str, Any]:
    """Evaluate budget utilization deterministically."""

    total = sum(
        (record.total_cost for record in records if record.currency == budget.currency),
        Decimal("0"),
    )
    utilization = Decimal("0") if budget.amount == 0 else (total / budget.amount) * Decimal("100")
    if budget.amount <= 0:
        status = "indeterminate"
    elif utilization >= budget.critical_threshold:
        status = "exceeded"
    elif utilization >= budget.warning_threshold:
        status = "warning"
    else:
        status = "healthy"
    return {
        "budget_id": budget.id,
        "status": status,
        "currency": budget.currency,
        "spend": money(total),
        "budget": money(budget.amount),
        "utilization_percent": money(utilization),
    }


def budget_hooks(records: Iterable[UsageRecord], budget: Budget) -> list[dict[str, Any]]:
    """Return deterministic budget hook actions for automation/policy integrations."""

    evaluation = evaluate_budget(records, budget)
    status = evaluation["status"]
    if status == "healthy":
        return []
    action = "notify"
    if status == "exceeded":
        action = "block_nonessential"
    elif status == "warning":
        action = "warn"
    return [
        {
            "budget_id": budget.id,
            "status": status,
            "action": action,
            "evidence": evaluation,
        }
    ]


def forecast_spend(records: Iterable[UsageRecord], period_days: int = 30) -> dict[str, Any]:
    """Simple deterministic linear run-rate forecast."""

    items = sorted(records, key=lambda item: item.data["start_time"])
    if len(items) < 2:
        return {"status": "insufficient_samples", "forecast": None}
    currencies = {item.currency for item in items}
    if len(currencies) != 1:
        raise OpenAIMeterError("cannot forecast mixed currencies")
    first = parse_time(str(items[0].data["start_time"]))
    last = parse_time(str(items[-1].data["start_time"]))
    elapsed_days = max(
        Decimal(str((last - first).total_seconds())) / Decimal("86400"), Decimal("1")
    )
    spend = sum((item.total_cost for item in items), Decimal("0"))
    forecast = spend / elapsed_days * Decimal(period_days)
    return {"status": "forecast", "currency": items[0].currency, "forecast": money(forecast)}


def detect_anomalies(records: Iterable[UsageRecord]) -> list[dict[str, Any]]:
    """Rule-based anomaly detection with evidence."""

    anomalies: list[dict[str, Any]] = []
    costs = [record.total_cost for record in records]
    threshold = (
        (sum(costs, Decimal("0")) / Decimal(len(costs)) * Decimal("3")) if costs else Decimal("0")
    )
    for record in records:
        usage = record.data.get("usage", {})
        if record.total_cost > threshold > 0:
            anomalies.append(
                {
                    "record_id": record.record_id,
                    "rule": "cost_spike",
                    "evidence": money(record.total_cost),
                }
            )
        if usage.get("total_tokens", 0) and decimal_from(
            usage.get("total_tokens", 0), "tokens"
        ) > Decimal("100000"):
            anomalies.append(
                {
                    "record_id": record.record_id,
                    "rule": "token_spike",
                    "evidence": usage["total_tokens"],
                }
            )
        if record.data.get("performance", {}).get("status") not in {None, "success"}:
            anomalies.append(
                {
                    "record_id": record.record_id,
                    "rule": "failure",
                    "evidence": record.data["performance"]["status"],
                }
            )
    return anomalies


class JsonlStore:
    """Local JSONL append-only storage."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: UsageRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = {item.record_id for item in self.read_all()} if self.path.exists() else set()
        if record.record_id in existing:
            raise OpenAIMeterError("duplicate record_id")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")

    def read_all(self) -> list[UsageRecord]:
        if not self.path.exists():
            return []
        return [
            validate_record(json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]


class SQLiteStore:
    """Local SQLite storage for usage records."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "record_id TEXT PRIMARY KEY, "
            "start_time TEXT NOT NULL, "
            "record_type TEXT NOT NULL, "
            "provider TEXT NOT NULL, "
            "model TEXT NOT NULL, "
            "currency TEXT NOT NULL, "
            "total_cost TEXT NOT NULL, success_weight TEXT NOT NULL, data TEXT NOT NULL)"
        )

    def close(self) -> None:
        self.connection.close()

    def add(self, record: UsageRecord) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.record_id,
                    record.data["start_time"],
                    record.data["record_type"],
                    record.data["model"]["provider"],
                    record.data["model"].get("response_model")
                    or record.data["model"].get("requested_model"),
                    record.currency,
                    str(record.total_cost),
                    str(record.success_weight),
                    record.to_json(),
                ),
            )

    def all(self) -> list[UsageRecord]:
        rows = self.connection.execute(
            "SELECT data FROM records ORDER BY start_time, record_id"
        ).fetchall()
        return [validate_record(json.loads(row[0])) for row in rows]

    def summarize(self) -> dict[str, Any]:
        records = self.all()
        total = sum((record.total_cost for record in records), Decimal("0"))
        cps = cost_per_success(records)
        return {
            "records": len(records),
            "currency": records[0].currency if records else "USD",
            "total_cost": money(total),
            "successful_outcomes": str(cps.denominator),
            "cost_per_success": None if cps.value is None else money(cps.value),
            "status": cps.status,
        }


class Meter:
    """High-level Python API for local OpenAIMeter usage."""

    def __init__(self, database: Path) -> None:
        self.store = SQLiteStore(database)

    def ingest(self, raw: dict[str, Any]) -> UsageRecord:
        record = validate_record(raw)
        self.store.add(record)
        return record

    def records(self) -> list[UsageRecord]:
        return self.store.all()

    def summarize(self) -> dict[str, Any]:
        return self.store.summarize()

    def close(self) -> None:
        self.store.close()


def load_yaml(path: Path) -> dict[str, Any]:
    """Safely load YAML from disk."""

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise OpenAIMeterError("YAML root must be a mapping")
    return loaded


def require_mapping(value: Any, context: str) -> dict[str, Any]:
    """Reject scalar/list inputs before record validation touches mapping fields."""

    if not isinstance(value, dict):
        raise OpenAIMeterError(f"{context} must be a JSON object")
    return value


def load_records(path: Path) -> list[UsageRecord]:
    """Load one record from JSON or many records from JSONL."""

    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [
            validate_record(require_mapping(json.loads(line), "JSONL record"))
            for line in text.splitlines()
            if line
        ]
    loaded = json.loads(text)
    if isinstance(loaded, list):
        return [validate_record(require_mapping(item, "JSON record")) for item in loaded]
    if isinstance(loaded, dict):
        return [validate_record(loaded)]
    raise OpenAIMeterError("expected JSON object or array")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def report(records: list[UsageRecord], kind: str) -> dict[str, Any]:
    """Generate deterministic JSON-compatible reports."""

    if kind == "cost-per-success":
        cps = cost_per_success(records)
        return {
            "metric": "cost_per_success",
            "status": cps.status,
            "currency": cps.currency,
            "numerator": money(cps.numerator),
            "denominator": str(cps.denominator),
            "value": None if cps.value is None else money(cps.value),
        }
    if kind == "outcomes":
        return {
            "attempts": len(records),
            "successful_outcomes": str(
                sum((record.success_weight for record in records), Decimal("0"))
            ),
        }
    if kind == "providers":
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for record in records:
            totals[str(record.data["model"]["provider"])] += record.total_cost
        return {"providers": {key: money(value) for key, value in sorted(totals.items())}}
    if kind == "anomalies":
        return {"anomalies": detect_anomalies(records)}
    return SQLiteSummary.from_records(records).as_dict()


def reconcile_costs(
    records: Iterable[UsageRecord], tolerance: Decimal = Decimal("0.000001")
) -> list[dict[str, Any]]:
    """Compare calculated, provider-reported, invoiced, and reconciled cost fields."""

    results: list[dict[str, Any]] = []
    for record in records:
        cost = record.data.get("cost", {})
        calculated = cost.get("provider_cost")
        reported = cost.get("provider_reported_cost")
        invoiced = cost.get("invoiced_cost")
        candidates = [
            ("calculated", decimal_from(calculated, "provider_cost"))
            if calculated is not None
            else None,
            ("provider_reported", decimal_from(reported, "provider_reported_cost"))
            if reported is not None
            else None,
            ("invoiced", decimal_from(invoiced, "invoiced_cost")) if invoiced is not None else None,
        ]
        present = [item for item in candidates if item is not None]
        if len(present) < 2:
            status = "insufficient_evidence"
            delta = None
        else:
            values = [value for _, value in present]
            max_delta = max(values) - min(values)
            status = "matched" if max_delta <= tolerance else "mismatch"
            delta = money(max_delta)
        results.append(
            {
                "record_id": record.record_id,
                "status": status,
                "delta": delta,
                "values": {name: money(value) for name, value in present},
            }
        )
    return results


def prometheus_escape(value: str) -> str:
    """Escape Prometheus label values."""

    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def export_prometheus_metrics(records: Iterable[UsageRecord]) -> str:
    """Export deterministic Prometheus text-format metrics."""

    items = list(records)
    lines = [
        "# HELP openaimeter_records_total Number of usage records.",
        "# TYPE openaimeter_records_total counter",
        f"openaimeter_records_total {len(items)}",
        "# HELP openaimeter_cost_total Total recorded cost by provider and model.",
        "# TYPE openaimeter_cost_total gauge",
    ]
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    successes: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for record in items:
        model = record.data["model"]
        model_name = str(model.get("response_model") or model.get("requested_model"))
        totals[(str(model["provider"]), model_name, record.currency)] += record.total_cost
        successes[(str(model["provider"]), model_name)] += record.success_weight
    for (provider, model_name, currency), value in sorted(totals.items()):
        lines.append(
            'openaimeter_cost_total{provider="'
            f'{prometheus_escape(provider)}",model="{prometheus_escape(model_name)}",'
            f'currency="{prometheus_escape(currency)}"}} {money(value)}'
        )
    lines.extend(
        [
            "# HELP openaimeter_successful_outcomes_total Successful outcome weight.",
            "# TYPE openaimeter_successful_outcomes_total gauge",
        ]
    )
    for (provider, model_name), value in sorted(successes.items()):
        lines.append(
            'openaimeter_successful_outcomes_total{provider="'
            f'{prometheus_escape(provider)}",model="{prometheus_escape(model_name)}"}} {value}'
        )
    return "\n".join(lines) + "\n"


def render_static_html_report(records: list[UsageRecord], title: str = "OpenAIMeter Report") -> str:
    """Render a self-contained static HTML report."""

    summary = SQLiteSummary.from_records(records).as_dict()
    provider_report = report(records, "providers")["providers"]
    cps = report(records, "cost-per-success")
    rows = []
    for record in records:
        model = record.data["model"]
        model_name = html.escape(str(model.get("response_model") or model.get("requested_model")))
        rows.append(
            "<tr>"
            f"<td>{html.escape(record.record_id)}</td>"
            f"<td>{html.escape(str(record.data['start_time']))}</td>"
            f"<td>{html.escape(str(model['provider']))}</td>"
            f"<td>{model_name}</td>"
            f"<td>{html.escape(record.currency)}</td>"
            f"<td>{html.escape(money(record.total_cost))}</td>"
            f"<td>{html.escape(str(record.success_weight))}</td>"
            "</tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;}"
        "table{border-collapse:collapse;width:100%;}td,th{border:1px solid #ddd;padding:.4rem;}"
        "th{background:#f5f5f5;text-align:left;}.metric{display:inline-block;margin-right:1rem;}</style>"
        "</head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class='metric'>Records: {summary['records']}</p>"
        f"<p class='metric'>Total cost: {summary['total_cost']} {summary['currency']}</p>"
        f"<p class='metric'>Cost per success: {cps['value']} {cps['currency']}</p>"
        f"<h2>Providers</h2><pre>{html.escape(json.dumps(provider_report, indent=2))}</pre>"
        "<h2>Records</h2><table><thead><tr><th>ID</th><th>Start</th><th>Provider</th>"
        "<th>Model</th><th>Currency</th><th>Cost</th><th>Success Weight</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )


@dataclass(frozen=True)
class SQLiteSummary:
    records: int
    currency: str
    total_cost: Decimal

    @classmethod
    def from_records(cls, records: list[UsageRecord]) -> Self:
        currency = records[0].currency if records else "USD"
        return cls(
            len(records), currency, sum((record.total_cost for record in records), Decimal("0"))
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "currency": self.currency,
            "total_cost": money(self.total_cost),
        }


def export_csv(records: list[UsageRecord], path: Path) -> None:
    """Export safe CSV records."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["record_id", "start_time", "provider", "model", "currency", "total_cost", "success"]
        )
        for record in records:
            model = record.data["model"]
            writer.writerow(
                [
                    safe_csv_cell(record.record_id),
                    safe_csv_cell(record.data["start_time"]),
                    safe_csv_cell(model["provider"]),
                    safe_csv_cell(model.get("response_model") or model.get("requested_model")),
                    record.currency,
                    safe_csv_cell(record.total_cost),
                    str(record.success_weight),
                ]
            )


def export_focus_rows(records: list[UsageRecord]) -> list[dict[str, Any]]:
    """Return a FOCUS-inspired, experimental cost export mapping."""

    rows: list[dict[str, Any]] = []
    for record in records:
        rows.append(
            {
                "ChargePeriodStart": record.data["start_time"],
                "ChargePeriodEnd": record.data["end_time"],
                "ProviderName": record.data["model"]["provider"],
                "ServiceName": "AI Model Usage",
                "ResourceName": record.data["model"].get("response_model")
                or record.data["model"].get("requested_model"),
                "BillingCurrency": record.currency,
                "EffectiveCost": money(record.total_cost),
                "Tags": json.dumps(record.data.get("attribution", {}), sort_keys=True),
            }
        )
    return rows


def replacement_savings(
    baseline: Decimal, candidate: Decimal, realized: bool
) -> dict[str, str | bool]:
    """Calculate model replacement savings with explicit realized/projected status."""

    return {
        "category": "realized" if realized else "benchmark_projected",
        "savings": money(baseline - candidate),
        "realized": realized,
    }


def cache_avoided_cost(uncached_cost: Decimal, cached_cost: Decimal) -> dict[str, str]:
    """Calculate cache avoided cost as an estimate unless reconciled elsewhere."""

    return {"category": "estimated", "avoided_cost": money(uncached_cost - cached_cost)}


def success_rate(records: Iterable[UsageRecord]) -> Decimal:
    """Calculate success rate."""

    items = list(records)
    if not items:
        return Decimal("0")
    return sum((item.success_weight for item in items), Decimal("0")) / Decimal(len(items))


def cache_hit_rate(records: Iterable[UsageRecord]) -> Decimal:
    """Calculate cache hit rate from record types."""

    items = list(records)
    cache_events = [
        item
        for item in items
        if item.data["record_type"] in {"model.cache_hit", "model.cache_miss"}
    ]
    if not cache_events:
        return Decimal("0")
    hits = sum(1 for item in cache_events if item.data["record_type"] == "model.cache_hit")
    return Decimal(hits) / Decimal(len(cache_events))


def audit_log_to_record(event: dict[str, Any]) -> UsageRecord:
    """Convert a portable audit-log-like event into an OpenAIMeter record."""

    actor = event.get("actor", {})
    target = event.get("target", {})
    usage = event.get("usage", {})
    cost = event.get("cost", {})
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": str(event.get("event_id") or event.get("record_id") or uuid.uuid4()),
        "record_type": str(event.get("record_type", "model.usage")),
        "start_time": str(event.get("start_time") or event.get("time")),
        "end_time": str(event.get("end_time") or event.get("time")),
        "source": {
            "service": event.get("service", "audit-log"),
            "component": event.get("component", "audit-log-adapter"),
            "environment": event.get("environment", "unknown"),
        },
        "attribution": {
            "organization_id": actor.get("organization_id"),
            "team_id": actor.get("team_id"),
            "project_id": actor.get("project_id"),
            "workflow_id": target.get("workflow_id") or event.get("workflow_id"),
            "agent_id": actor.get("agent_id"),
            "tenant_id": actor.get("tenant_id"),
        },
        "model": {
            "provider": target.get("provider") or event.get("provider", "unknown"),
            "requested_model": target.get("model") or event.get("model", "unknown"),
            "response_model": target.get("model") or event.get("model", "unknown"),
            "hosting": target.get("hosting", "unknown"),
        },
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens", 0),
            "cached_output_tokens": usage.get("cached_output_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
            "audio_input_tokens": usage.get("audio_input_tokens", 0),
            "audio_output_tokens": usage.get("audio_output_tokens", 0),
            "image_input_units": usage.get("image_input_units", 0),
            "total_tokens": usage.get("total_tokens"),
        },
        "performance": {
            "latency_ms": event.get("latency_ms", 0),
            "status": event.get("status", "success"),
        },
        "cost": {
            "currency": cost.get("currency", "USD"),
            "provider_cost": cost.get("provider_cost", "0"),
            "infrastructure_cost": cost.get("infrastructure_cost", "0"),
            "allocated_cost": cost.get("allocated_cost", "0"),
            "total_cost": cost.get("total_cost", "0"),
            "calculation_method": cost.get("calculation_method", "audit_log_derived"),
        },
        "outcome": event.get("outcome", {}),
        "correlation": event.get("correlation", {}),
        "metadata": {"source_event_type": event.get("event_type", "audit_log")},
    }
    return validate_record(record)


def load_audit_log(path: Path) -> list[UsageRecord]:
    """Load JSON or JSONL audit-log events and convert them to usage records."""

    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        events = [
            require_mapping(json.loads(line), "audit-log event")
            for line in text.splitlines()
            if line
        ]
    else:
        loaded = json.loads(text)
        events = (
            [require_mapping(item, "audit-log event") for item in loaded]
            if isinstance(loaded, list)
            else [require_mapping(loaded, "audit-log event")]
        )
    return [audit_log_to_record(event) for event in events]


def orchestration_record(
    *,
    run_id: str,
    agent_id: str,
    workflow_id: str,
    start_time: datetime,
    end_time: datetime,
    status: str,
    cost: Decimal = Decimal("0"),
    currency: str = "USD",
) -> UsageRecord:
    """Create an agent-run instrumentation record for orchestration systems."""

    latency = Decimal(str((end_time - start_time).total_seconds())) * Decimal("1000")
    return validate_record(
        {
            "schema_version": SCHEMA_VERSION,
            "record_id": f"agent-run-{run_id}",
            "record_type": "agent.run",
            "start_time": isoformat(start_time),
            "end_time": isoformat(end_time),
            "source": {"service": "orchestration", "component": "openaimeter"},
            "attribution": {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "agent_id": agent_id,
            },
            "model": {"provider": "orchestrator", "requested_model": "agent", "hosting": "unknown"},
            "usage": {"total_tokens": 0},
            "performance": {"latency_ms": int(latency), "status": status},
            "cost": {"currency": currency, "total_cost": money(cost)},
            "outcome": {"outcome_id": workflow_id, "success": status == "success", "quantity": "1"},
            "metadata": {"instrumentation": "orchestration_record"},
        }
    )


def local_cost_profile_catalog() -> dict[str, dict[str, Any]]:
    """Return built-in local-cost profile templates with explicit assumptions."""

    return {
        "local-rtx-workstation": {
            "profile": {
                "id": "local-rtx-workstation",
                "currency": "USD",
                "compute": {
                    "gpu_hourly_cost": "0.18",
                    "cpu_hourly_cost": "0.04",
                    "memory_hourly_cost": "0.01",
                },
                "energy": {"average_watts": "280", "electricity_per_kwh": "0.16"},
                "allocation": {"overhead_percentage": "10"},
                "notes": "Example template; replace with organization-specific assumptions.",
            }
        },
        "cloud-gpu-hourly": {
            "profile": {
                "id": "cloud-gpu-hourly",
                "currency": "USD",
                "compute": {
                    "gpu_hourly_cost": "1.20",
                    "cpu_hourly_cost": "0.08",
                    "memory_hourly_cost": "0.03",
                },
                "allocation": {"overhead_percentage": "0"},
                "notes": "Example cloud GPU template; not live provider pricing.",
            }
        },
    }


def model_swap_projection(data: dict[str, Any]) -> dict[str, Any]:
    """Project ModelSwapBench-style replacement economics without calling it realized."""

    baseline = data["baseline"]
    candidate = data["candidate"]
    volume = decimal_from(data.get("volume", "1"), "volume")
    baseline_cps = decimal_from(baseline["cost_per_success"], "baseline.cost_per_success")
    candidate_cps = decimal_from(candidate["cost_per_success"], "candidate.cost_per_success")
    baseline_success = decimal_from(baseline["success_rate"], "baseline.success_rate")
    candidate_success = decimal_from(candidate["success_rate"], "candidate.success_rate")
    quality_delta = candidate_success - baseline_success
    projected = (baseline_cps - candidate_cps) * volume
    return {
        "category": "benchmark_projected",
        "realized": False,
        "currency": data.get("currency", "USD"),
        "baseline_model": baseline.get("model"),
        "candidate_model": candidate.get("model"),
        "baseline_cost_per_success": money(baseline_cps),
        "candidate_cost_per_success": money(candidate_cps),
        "quality_delta": str(quality_delta),
        "volume": str(volume),
        "projected_savings": money(projected),
    }


def apply_ontology_attribution(record: UsageRecord, ontology: dict[str, Any]) -> UsageRecord:
    """Apply ontology-based team/project/workflow attribution mappings."""

    updated = json.loads(record.to_json())
    attribution = updated.setdefault("attribution", {})
    mappings = ontology.get("mappings", {})
    workflow_id = attribution.get("workflow_id")
    project_id = attribution.get("project_id")
    if workflow_id in mappings.get("workflows", {}):
        attribution.update(mappings["workflows"][workflow_id])
    if project_id in mappings.get("projects", {}):
        attribution.update(mappings["projects"][project_id])
    updated["metadata"] = updated.get("metadata") or {}
    updated["metadata"]["ontology_attribution"] = ontology.get("id", "supplied-ontology")
    return validate_record(updated)


def finite_or_none(value: Decimal) -> str | None:
    """Return a string only for finite Decimal values."""

    return None if not math.isfinite(float(value)) else str(value)
