"""Core measurement, accounting, storage, and reporting primitives."""

from __future__ import annotations

import copy
import csv
import html
import json
import math
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol, Self

import yaml
from yaml.resolver import BaseResolver

VERSION = "0.2.0b1"
SCHEMA_VERSION = "1.0"
MAX_METADATA_DEPTH = 8
MAX_RECORD_BYTES = 256_000
MAX_INPUT_BYTES = 4_000_000
MAX_RECORDS_PER_INPUT = 10_000
MAX_PRICING_ENTRIES = 10_000
MAX_STRING_BYTES = 32_000
MAX_CONTAINER_ITEMS = 10_000
MAX_STRUCTURE_DEPTH = 32
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


class AIMeterError(ValueError):
    """Base validation or accounting error."""


class _DuplicateKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _DuplicateKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise AIMeterError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise AIMeterError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class Adapter(Protocol):
    """Protocol for optional integrations that produce usage records."""

    def records(self) -> Iterable[dict[str, Any]]:
        """Yield AIMeter-compatible usage records."""


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
        raise AIMeterError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise AIMeterError("timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def isoformat(value: datetime) -> str:
    """Serialize a datetime as RFC 3339 UTC."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def decimal_from(value: Any, field: str) -> Decimal:
    """Parse a Decimal from JSON/YAML-safe values."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AIMeterError(f"{field} must be a valid Decimal") from exc
    if not parsed.is_finite():
        raise AIMeterError(f"{field} must be a finite Decimal")
    return parsed


def money(value: Decimal) -> str:
    """Stable six-place money string for reports."""

    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def require_currency(value: Any) -> str:
    """Validate a three-letter supported currency."""

    currency = str(value)
    if currency not in VALID_CURRENCIES:
        raise AIMeterError(f"unsupported currency: {currency}")
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


def validate_structure(value: Any, context: str) -> None:
    """Apply bounded, JSON-compatible structure limits to untrusted local input."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_STRUCTURE_DEPTH:
            raise AIMeterError(f"{context} nesting exceeds safety limit")
        if isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_STRING_BYTES:
                raise AIMeterError(f"{context} contains an oversized string")
        elif isinstance(current, dict):
            if len(current) > MAX_CONTAINER_ITEMS:
                raise AIMeterError(f"{context} contains too many mapping items")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if len(current) > MAX_CONTAINER_ITEMS:
                raise AIMeterError(f"{context} contains too many list items")
            stack.extend((item, depth + 1) for item in current)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AIMeterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_value(text: str, context: str) -> Any:
    """Parse JSON while rejecting duplicate object keys."""

    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise AIMeterError(f"invalid JSON {context}: {exc.msg}") from exc


def read_limited_text(path: Path, limit: int, context: str) -> str:
    """Read UTF-8 input with a deterministic byte limit."""

    try:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError as exc:
        raise AIMeterError(f"unable to read {context}: {exc}") from exc
    if len(content) > limit:
        raise AIMeterError(f"{context} exceeds {limit} byte safety limit")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AIMeterError(f"{context} must be valid UTF-8") from exc


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
    def total_cost(self) -> Decimal | None:
        """Return a known total cost, or ``None`` when the record is incomplete."""

        value = self.data.get("cost", {}).get("total_cost")
        return None if value is None else decimal_from(value, "cost.total_cost")

    @property
    def has_known_cost(self) -> bool:
        """Whether this record carries a known total cost."""

        return self.total_cost is not None

    @property
    def has_known_outcome(self) -> bool:
        """Whether the record supplies enough information to evaluate success."""

        outcome = self.data.get("outcome") or {}
        if outcome.get("correction_status") == "invalidated":
            return True
        if isinstance(outcome.get("success"), bool):
            return True
        return outcome.get("score") is not None and outcome.get("threshold") is not None

    @property
    def success_weight(self) -> Decimal | None:
        outcome = self.data.get("outcome") or {}
        if outcome.get("correction_status") == "invalidated":
            return Decimal("0")
        if outcome.get("success") is True:
            return decimal_from(outcome.get("quantity", "1"), "outcome.quantity")
        if outcome.get("success") is False:
            return Decimal("0")
        score = outcome.get("score")
        threshold = outcome.get("threshold")
        if score is not None and threshold is not None:
            score_d = decimal_from(score, "outcome.score")
            return (
                Decimal("1")
                if score_d >= decimal_from(threshold, "outcome.threshold")
                else Decimal("0")
            )
        return None

    @property
    def latency_ms(self) -> Decimal | None:
        value = self.data.get("performance", {}).get("latency_ms")
        return None if value is None else decimal_from(value, "latency_ms")

    def to_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, separators=(",", ":"), default=str)


def validate_record(raw: dict[str, Any]) -> UsageRecord:
    """Validate and normalize a usage record without changing its economic meaning."""

    if not isinstance(raw, dict):
        raise AIMeterError("record must be a JSON object")
    try:
        normalized = copy.deepcopy(raw)
        encoded_len = len(json.dumps(normalized, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise AIMeterError("record must be JSON-serializable") from exc
    if encoded_len > MAX_RECORD_BYTES:
        raise AIMeterError("record is too large")
    validate_structure(normalized, "record")
    if normalized.get("schema_version") != SCHEMA_VERSION:
        raise AIMeterError("schema_version must be 1.0")
    if not isinstance(normalized.get("record_id"), str) or not normalized["record_id"]:
        raise AIMeterError("record_id is required")
    record_type = str(normalized.get("record_type", ""))
    if record_type not in RECORD_TYPES and not re.match(
        r"^[a-z][a-z0-9_]*\.[a-z0-9_.-]+$", record_type
    ):
        raise AIMeterError(f"unsupported record_type: {record_type}")
    if not normalized.get("start_time") or not normalized.get("end_time"):
        raise AIMeterError("start_time and end_time are required")
    start = parse_time(str(normalized["start_time"]))
    end = parse_time(str(normalized["end_time"]))
    if end < start:
        raise AIMeterError("end_time must not be before start_time")
    model = require_mapping(normalized.get("model"), "model")
    if not model.get("provider"):
        raise AIMeterError("model.provider is required")
    if not model.get("requested_model"):
        raise AIMeterError("model.requested_model is required")
    hosting = str(model.get("hosting", "unknown"))
    if hosting not in HOSTING_MODES:
        raise AIMeterError(f"unsupported hosting mode: {hosting}")
    usage = require_mapping(normalized.get("usage"), "usage")
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
            raise AIMeterError(f"usage.{key} must be a nonnegative integer")
        known_total += value
    total = usage.get("total_tokens")
    if total is not None:
        if not isinstance(total, int) or total < 0:
            raise AIMeterError("usage.total_tokens must be a nonnegative integer")
        semantics = usage.get("provider_total_semantics")
        if all_components_present and total != known_total and semantics is None:
            raise AIMeterError("usage.total_tokens is inconsistent with supplied components")
    require_mapping(normalized.get("performance"), "performance")
    cost = require_mapping(normalized.get("cost"), "cost")
    require_currency(cost.get("currency", "USD"))
    for key in ("provider_cost", "infrastructure_cost", "allocated_cost", "total_cost"):
        if key in cost and cost[key] is not None:
            decimal_from(cost[key], f"cost.{key}")
    metadata = normalized.get("metadata") or {}
    require_mapping(metadata, "metadata")
    if metadata_depth(metadata) > MAX_METADATA_DEPTH:
        raise AIMeterError("metadata depth exceeds safety limit")
    return UsageRecord(normalized)


@dataclass(frozen=True)
class PricingTable:
    """Versioned local pricing table with deterministic resolution."""

    version: str
    entries: list[dict[str, Any]]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise AIMeterError("pricing version is required")
        if not isinstance(self.entries, list) or len(self.entries) > MAX_PRICING_ENTRIES:
            raise AIMeterError("pricing entries exceed safety limit")
        validated: list[dict[str, Any]] = []
        for index, raw_entry in enumerate(self.entries):
            entry = require_mapping(raw_entry, f"pricing entry {index}")
            required = ("provider", "model_key", "currency", "effective_start", "source_reference")
            if any(not entry.get(key) for key in required):
                raise AIMeterError(
                    f"pricing entry {index} is missing required provenance or identity"
                )
            require_currency(entry["currency"])
            start = parse_time(str(entry["effective_start"]))
            end_raw = entry.get("effective_end")
            if end_raw is not None and parse_time(str(end_raw)) <= start:
                raise AIMeterError(f"pricing entry {index} has an invalid effective range")
            if entry.get("verified_date") is not None:
                verified = str(entry["verified_date"])
                parse_time(f"{verified}T00:00:00Z" if len(verified) == 10 else verified)
            if entry.get("expires_at") is not None:
                parse_time(str(entry["expires_at"]))
            rate_fields = (
                "input_token_price",
                "output_token_price",
                "cached_input_price",
                "cached_output_price",
                "reasoning_token_price",
                "per_request_price",
            )
            if not any(entry.get(field) is not None for field in rate_fields):
                raise AIMeterError(f"pricing entry {index} has no billable rates")
            for field in rate_fields:
                if entry.get(field) is not None and decimal_from(entry[field], field) < 0:
                    raise AIMeterError(f"pricing entry {index} has a negative rate")
            discount = decimal_from(entry.get("batch_discount", "0"), "batch_discount")
            if not Decimal("0") <= discount <= Decimal("100"):
                raise AIMeterError("batch_discount must be between 0 and 100")
            validated.append(copy.deepcopy(entry))
        self._validate_no_overlaps(validated)
        object.__setattr__(self, "entries", validated)

    @staticmethod
    def _validate_no_overlaps(entries: list[dict[str, Any]]) -> None:
        for index, entry in enumerate(entries):
            start = parse_time(str(entry["effective_start"]))
            end = parse_time(str(entry["effective_end"])) if entry.get("effective_end") else None
            identity = (
                entry["provider"],
                entry["model_key"],
                entry["currency"],
                entry.get("region"),
            )
            for other in entries[index + 1 :]:
                other_identity = (
                    other["provider"],
                    other["model_key"],
                    other["currency"],
                    other.get("region"),
                )
                if identity != other_identity:
                    continue
                other_start = parse_time(str(other["effective_start"]))
                other_end = (
                    parse_time(str(other["effective_end"])) if other.get("effective_end") else None
                )
                if (end is None or other_start < end) and (other_end is None or start < other_end):
                    raise AIMeterError("pricing entries have overlapping effective ranges")

    @classmethod
    def from_file(cls, path: Path) -> Self:
        data = load_yaml(path)
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise AIMeterError("pricing entries must be a list")
        return cls(version=str(data.get("version", "")), entries=entries)

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
        matching_identity = [
            entry
            for entry in self.entries
            if entry.get("provider") == model.get("provider")
            and entry.get("model_key")
            in {model.get("pricing_key"), model.get("response_model"), model.get("requested_model")}
            and entry.get("region") in {None, model.get("region")}
            and entry.get("currency") == cost_currency
        ]
        if matching_identity:
            return "outside_validity_period", None
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
    missing_usage = [
        usage_field
        for usage_field, rate_field in token_rates.values()
        if entry.get(rate_field) is not None and usage.get(usage_field) is None
    ]
    if missing_usage:
        return {
            "status": "unknown",
            "provider_cost": None,
            "resolution": "missing_usage",
            "pricing_version": pricing.version,
            "missing_usage": missing_usage,
            "components": {},
        }
    missing_rates = [
        rate_field
        for usage_field, rate_field in token_rates.values()
        if usage.get(usage_field) is not None
        and decimal_from(usage[usage_field], usage_field) > 0
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
        "pricing_provenance": {
            "source_reference": entry["source_reference"],
            "source_type": entry.get("source_type", "unknown"),
            "verified_date": entry.get("verified_date"),
            "effective_start": entry["effective_start"],
            "effective_end": entry.get("effective_end"),
            "expires_at": entry.get("expires_at"),
        },
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
        raise AIMeterError("record and local profile currencies differ")
    perf = record.data.get("performance", {})
    duration_ms = decimal_from(perf.get("latency_ms"), "performance.latency_ms")
    if duration_ms <= 0:
        raise AIMeterError("local inference duration is required")
    hours = duration_ms / Decimal("3600000")
    compute = profile_data.get("compute", {})
    energy = profile_data.get("energy", {})
    allocation = profile_data.get("allocation", {})
    required = ["gpu_hourly_cost", "cpu_hourly_cost", "memory_hourly_cost"]
    if any(compute.get(key) is None for key in required):
        raise AIMeterError("missing local compute assumptions")
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
        raise AIMeterError("allocation weights must be nonnegative")
    denominator = sum(allocation.weights, Decimal("0"))
    if denominator <= 0:
        raise AIMeterError("allocation denominator must be positive")
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
        if any(item.latency_ms is None for item in items):
            raise AIMeterError("duration allocation requires known latency for every record")
        return [max(item.latency_ms or Decimal("0"), Decimal("0")) for item in items]
    if method == "token_count":
        if any(item.data.get("usage", {}).get("total_tokens") is None for item in items):
            raise AIMeterError("token allocation requires known total_tokens for every record")
        return [
            decimal_from(item.data["usage"]["total_tokens"], "usage.total_tokens") for item in items
        ]
    if method == "successful_outcome":
        if any(item.success_weight is None for item in items):
            raise AIMeterError(
                "successful outcome allocation requires known outcomes for every record"
            )
        return [item.success_weight or Decimal("0") for item in items]
    if method == "workflow_weight":
        return [
            decimal_from(
                item.data.get("attribution", {}).get("workflow_weight", "1"), "workflow_weight"
            )
            for item in items
        ]
    raise AIMeterError(f"unsupported allocation method: {method}")


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
    unknown_cost_records: int = 0
    unknown_outcome_records: int = 0

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
        raise AIMeterError("cannot combine currencies")
    known_costs = [item.total_cost for item in items if item.total_cost is not None]
    known_outcomes = [item.success_weight for item in items if item.success_weight is not None]
    unknown_cost_records = len(items) - len(known_costs)
    unknown_outcome_records = len(items) - len(known_outcomes)
    numerator = sum(known_costs, Decimal("0"))
    denominator = sum(known_outcomes, Decimal("0"))
    currency = currencies.pop()
    if unknown_cost_records:
        return CostPerSuccessResult(
            numerator,
            denominator,
            "incomplete_cost_data",
            currency,
            unknown_cost_records,
            unknown_outcome_records,
        )
    if unknown_outcome_records:
        return CostPerSuccessResult(
            numerator,
            denominator,
            "incomplete_outcome_data",
            currency,
            unknown_cost_records,
            unknown_outcome_records,
        )
    if denominator == 0:
        return CostPerSuccessResult(numerator, denominator, "zero_successes", currency)
    return CostPerSuccessResult(numerator, denominator, "defined", currency)


@dataclass(frozen=True)
class CostSummary:
    """A cost aggregate that preserves whether all included costs are known."""

    records: int
    currency: str
    known_total_cost: Decimal
    unknown_cost_records: int

    @property
    def status(self) -> str:
        return "complete" if self.unknown_cost_records == 0 else "incomplete_cost_data"

    @property
    def total_cost(self) -> Decimal | None:
        return self.known_total_cost if self.status == "complete" else None


def summarize_costs(records: Iterable[UsageRecord]) -> CostSummary:
    """Summarize costs without representing unknown records as zero."""

    items = list(records)
    if not items:
        return CostSummary(0, "USD", Decimal("0"), 0)
    currencies = {item.currency for item in items}
    if len(currencies) != 1:
        raise AIMeterError("cannot combine currencies")
    known = [item.total_cost for item in items if item.total_cost is not None]
    return CostSummary(
        len(items), currencies.pop(), sum(known, Decimal("0")), len(items) - len(known)
    )


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

    matching = [record for record in records if record.currency == budget.currency]
    summary = summarize_costs(matching)
    total = summary.known_total_cost
    if summary.unknown_cost_records:
        return {
            "budget_id": budget.id,
            "status": "incomplete_cost_data",
            "currency": budget.currency,
            "spend": None,
            "known_spend": money(total),
            "unknown_cost_records": summary.unknown_cost_records,
            "budget": money(budget.amount),
            "utilization_percent": None,
        }
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
        raise AIMeterError("cannot forecast mixed currencies")
    summary = summarize_costs(items)
    if summary.unknown_cost_records:
        return {
            "status": "incomplete_cost_data",
            "forecast": None,
            "known_spend": money(summary.known_total_cost),
            "unknown_cost_records": summary.unknown_cost_records,
        }
    first = parse_time(str(items[0].data["start_time"]))
    last = parse_time(str(items[-1].data["start_time"]))
    elapsed_days = max(
        Decimal(str((last - first).total_seconds())) / Decimal("86400"), Decimal("1")
    )
    spend = summary.known_total_cost
    forecast = spend / elapsed_days * Decimal(period_days)
    return {"status": "forecast", "currency": items[0].currency, "forecast": money(forecast)}


def detect_anomalies(records: Iterable[UsageRecord]) -> list[dict[str, Any]]:
    """Rule-based anomaly detection with evidence."""

    anomalies: list[dict[str, Any]] = []
    items = list(records)
    costs = [record.total_cost for record in items if record.total_cost is not None]
    threshold = (
        (sum(costs, Decimal("0")) / Decimal(len(costs)) * Decimal("3")) if costs else Decimal("0")
    )
    for record in items:
        usage = record.data.get("usage", {})
        if record.total_cost is None:
            anomalies.append(
                {"record_id": record.record_id, "rule": "unknown_cost", "evidence": "unknown"}
            )
        elif record.total_cost > threshold > 0:
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
            raise AIMeterError("duplicate record_id")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")

    def read_all(self) -> list[UsageRecord]:
        if not self.path.exists():
            return []
        return load_records(self.path)


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
            "total_cost TEXT, success_weight TEXT, data TEXT NOT NULL)"
        )
        self._migrate_legacy_schema()

    def _migrate_legacy_schema(self) -> None:
        columns = {
            str(row[1]): bool(row[3])
            for row in self.connection.execute("PRAGMA table_info(records)").fetchall()
        }
        if not columns.get("total_cost", False) and not columns.get("success_weight", False):
            return
        with self.connection:
            self.connection.execute("ALTER TABLE records RENAME TO records_legacy")
            self.connection.execute(
                "CREATE TABLE records ("
                "record_id TEXT PRIMARY KEY, start_time TEXT NOT NULL, record_type TEXT NOT NULL, "
                "provider TEXT NOT NULL, model TEXT NOT NULL, currency TEXT NOT NULL, "
                "total_cost TEXT, success_weight TEXT, data TEXT NOT NULL)"
            )
            rows = self.connection.execute("SELECT data FROM records_legacy").fetchall()
            for (raw_data,) in rows:
                self._insert(validate_record(load_json_value(str(raw_data), "stored record")))
            self.connection.execute("DROP TABLE records_legacy")

    def close(self) -> None:
        self.connection.close()

    def add(self, record: UsageRecord) -> None:
        self.add_many([record])

    def _insert(self, record: UsageRecord) -> None:
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
                None if record.total_cost is None else str(record.total_cost),
                None if record.success_weight is None else str(record.success_weight),
                record.to_json(),
            ),
        )

    def add_many(self, records: Iterable[UsageRecord]) -> None:
        items = list(records)
        ensure_unique_record_ids(items)
        try:
            with self.connection:
                for record in items:
                    self._insert(record)
        except sqlite3.IntegrityError as exc:
            raise AIMeterError("duplicate record_id") from exc

    def all(self) -> list[UsageRecord]:
        rows = self.connection.execute(
            "SELECT data FROM records ORDER BY start_time, record_id"
        ).fetchall()
        return [validate_record(load_json_value(str(row[0]), "stored record")) for row in rows]

    def summarize(self) -> dict[str, Any]:
        records = self.all()
        summary = summarize_costs(records)
        cps = cost_per_success(records)
        return {
            "records": len(records),
            "currency": summary.currency,
            "status": summary.status,
            "total_cost": None if summary.total_cost is None else money(summary.total_cost),
            "known_total_cost": money(summary.known_total_cost),
            "unknown_cost_records": summary.unknown_cost_records,
            "successful_outcomes": str(cps.denominator),
            "cost_per_success": None if cps.value is None else money(cps.value),
            "cost_per_success_status": cps.status,
        }


class Meter:
    """High-level Python API for local AIMeter usage."""

    def __init__(self, database: Path) -> None:
        self.store = SQLiteStore(database)

    def ingest(self, raw: dict[str, Any]) -> UsageRecord:
        record = validate_record(raw)
        self.store.add(record)
        return record

    def ingest_many(self, raw_records: Iterable[dict[str, Any]]) -> list[UsageRecord]:
        """Validate and persist a complete batch atomically."""

        records = [validate_record(raw) for raw in raw_records]
        self.store.add_many(records)
        return records

    def records(self) -> list[UsageRecord]:
        return self.store.all()

    def summarize(self) -> dict[str, Any]:
        return self.store.summarize()

    def close(self) -> None:
        self.store.close()


def load_yaml(path: Path) -> dict[str, Any]:
    """Safely load YAML from disk."""

    text = read_limited_text(path, MAX_INPUT_BYTES, "YAML input")
    try:
        loaded = yaml.load(text, Loader=_DuplicateKeyLoader)  # noqa: S506  # nosec B506
    except yaml.YAMLError as exc:
        raise AIMeterError(f"invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise AIMeterError("YAML root must be a mapping")
    validate_structure(loaded, "YAML input")
    return loaded


def require_mapping(value: Any, context: str) -> dict[str, Any]:
    """Reject scalar/list inputs before record validation touches mapping fields."""

    if not isinstance(value, dict):
        raise AIMeterError(f"{context} must be a JSON object")
    return value


def load_records(path: Path) -> list[UsageRecord]:
    """Load one record from JSON or many records from JSONL."""

    text = read_limited_text(path, MAX_INPUT_BYTES, "JSON input")
    if path.suffix == ".jsonl":
        lines = [line for line in text.splitlines() if line]
        if len(lines) > MAX_RECORDS_PER_INPUT:
            raise AIMeterError("JSON input contains too many records")
        records = [
            validate_record(require_mapping(load_json_value(line, "JSONL record"), "JSONL record"))
            for line in lines
        ]
        ensure_unique_record_ids(records)
        return records
    loaded = load_json_value(text, "input")
    if isinstance(loaded, list):
        if len(loaded) > MAX_RECORDS_PER_INPUT:
            raise AIMeterError("JSON input contains too many records")
        records = [validate_record(require_mapping(item, "JSON record")) for item in loaded]
        ensure_unique_record_ids(records)
        return records
    if isinstance(loaded, dict):
        return [validate_record(loaded)]
    raise AIMeterError("expected JSON object or array")


def load_single_record(path: Path) -> UsageRecord:
    """Load exactly one record for a single-record calculator."""

    records = load_records(path)
    if len(records) != 1:
        raise AIMeterError("command requires exactly one record")
    return records[0]


def ensure_unique_record_ids(records: Iterable[UsageRecord]) -> None:
    """Reject duplicate event IDs before they can distort aggregates."""

    identifiers: set[str] = set()
    for count, record in enumerate(records, start=1):
        if count > MAX_RECORDS_PER_INPUT:
            raise AIMeterError("input contains too many records")
        if record.record_id in identifiers:
            raise AIMeterError("duplicate record_id")
        identifiers.add(record.record_id)


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
            "unknown_cost_records": cps.unknown_cost_records,
            "unknown_outcome_records": cps.unknown_outcome_records,
        }
    if kind == "outcomes":
        known = [record.success_weight for record in records if record.success_weight is not None]
        return {
            "attempts": len(records),
            "successful_outcomes": str(sum(known, Decimal("0"))),
            "unknown_outcome_records": len(records) - len(known),
            "status": "complete" if len(known) == len(records) else "incomplete_outcome_data",
        }
    if kind == "providers":
        grouped: dict[tuple[str, str], list[UsageRecord]] = defaultdict(list)
        for record in records:
            grouped[(str(record.data["model"]["provider"]), record.currency)].append(record)
        providers: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for (provider, currency), items in grouped.items():
            summary = summarize_costs(items)
            providers[provider][currency] = {
                "status": summary.status,
                "total_cost": None if summary.total_cost is None else money(summary.total_cost),
                "known_total_cost": money(summary.known_total_cost),
                "unknown_cost_records": summary.unknown_cost_records,
            }
        return {
            "providers": {
                provider: dict(sorted(currencies.items()))
                for provider, currencies in sorted(providers.items())
            }
        }
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
        "# HELP aimeter_records_total Number of usage records.",
        "# TYPE aimeter_records_total counter",
        f"aimeter_records_total {len(items)}",
        "# HELP aimeter_cost_total Total recorded cost by provider and model.",
        "# TYPE aimeter_cost_total gauge",
    ]
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    successes: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for record in items:
        model = record.data["model"]
        model_name = str(model.get("response_model") or model.get("requested_model"))
        if record.total_cost is not None:
            totals[(str(model["provider"]), model_name, record.currency)] += record.total_cost
        if record.success_weight is not None:
            successes[(str(model["provider"]), model_name)] += record.success_weight
    for (provider, model_name, currency), value in sorted(totals.items()):
        lines.append(
            'aimeter_cost_total{provider="'
            f'{prometheus_escape(provider)}",model="{prometheus_escape(model_name)}",'
            f'currency="{prometheus_escape(currency)}"}} {money(value)}'
        )
    lines.extend(
        [
            "# HELP aimeter_successful_outcomes_total Successful outcome weight.",
            "# TYPE aimeter_successful_outcomes_total gauge",
        ]
    )
    for (provider, model_name), value in sorted(successes.items()):
        lines.append(
            'aimeter_successful_outcomes_total{provider="'
            f'{prometheus_escape(provider)}",model="{prometheus_escape(model_name)}"}} {value}'
        )
    return "\n".join(lines) + "\n"


def render_static_html_report(records: list[UsageRecord], title: str = "AIMeter Report") -> str:
    """Render a self-contained static HTML report."""

    summary = SQLiteSummary.from_records(records).as_dict()
    provider_report = report(records, "providers")["providers"]
    cps = report(records, "cost-per-success")
    rows = []
    for record in records:
        model = record.data["model"]
        model_name = html.escape(str(model.get("response_model") or model.get("requested_model")))
        cost_text = "unknown" if record.total_cost is None else money(record.total_cost)
        outcome_text = "unknown" if record.success_weight is None else str(record.success_weight)
        rows.append(
            "<tr>"
            f"<td>{html.escape(record.record_id)}</td>"
            f"<td>{html.escape(str(record.data['start_time']))}</td>"
            f"<td>{html.escape(str(model['provider']))}</td>"
            f"<td>{model_name}</td>"
            f"<td>{html.escape(record.currency)}</td>"
            f"<td>{html.escape(cost_text)}</td>"
            f"<td>{html.escape(outcome_text)}</td>"
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
        f"<p class='metric'>Known cost subtotal: {summary['known_total_cost']} "
        f"{summary['currency']}</p>"
        f"<p class='metric'>Cost completeness: {summary['status']} "
        f"({summary['unknown_cost_records']} unknown records)</p>"
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
    total_cost: Decimal | None
    known_total_cost: Decimal
    unknown_cost_records: int

    @classmethod
    def from_records(cls, records: list[UsageRecord]) -> Self:
        summary = summarize_costs(records)
        return cls(
            summary.records,
            summary.currency,
            summary.total_cost,
            summary.known_total_cost,
            summary.unknown_cost_records,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "currency": self.currency,
            "total_cost": None if self.total_cost is None else money(self.total_cost),
            "known_total_cost": money(self.known_total_cost),
            "unknown_cost_records": self.unknown_cost_records,
            "status": "complete" if self.unknown_cost_records == 0 else "incomplete_cost_data",
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
                    "" if record.success_weight is None else str(record.success_weight),
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
                "EffectiveCost": None if record.total_cost is None else money(record.total_cost),
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


def success_rate(records: Iterable[UsageRecord]) -> Decimal | None:
    """Calculate success rate, returning ``None`` when outcomes are incomplete."""

    items = list(records)
    if not items:
        return Decimal("0")
    if any(item.success_weight is None for item in items):
        return None
    return sum((item.success_weight or Decimal("0") for item in items), Decimal("0")) / Decimal(
        len(items)
    )


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
    """Convert a portable audit-log-like event into an AIMeter record."""

    actor = event.get("actor", {})
    target = event.get("target", {})
    usage = event.get("usage", {})
    cost = event.get("cost", {})
    source_id = event.get("event_id") or event.get("record_id")
    if not source_id:
        raise AIMeterError("audit-log event_id or record_id is required")
    if not event.get("start_time") and not event.get("time"):
        raise AIMeterError("audit-log event time is required")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": str(source_id),
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
            "calculation_method": cost.get("calculation_method", "audit_log_derived"),
        },
        "outcome": event.get("outcome", {}),
        "correlation": event.get("correlation", {}),
        "metadata": {"source_event_type": event.get("event_type", "audit_log")},
    }
    for field in ("provider_cost", "infrastructure_cost", "allocated_cost", "total_cost"):
        if cost.get(field) is not None:
            record["cost"][field] = cost[field]
    return validate_record(record)


def load_audit_log(path: Path) -> list[UsageRecord]:
    """Load JSON or JSONL audit-log events and convert them to usage records."""

    text = read_limited_text(path, MAX_INPUT_BYTES, "audit-log input")
    if path.suffix == ".jsonl":
        lines = [line for line in text.splitlines() if line]
        if len(lines) > MAX_RECORDS_PER_INPUT:
            raise AIMeterError("audit-log input contains too many events")
        events = [
            require_mapping(load_json_value(line, "audit-log event"), "audit-log event")
            for line in lines
        ]
    else:
        loaded = load_json_value(text, "audit-log input")
        events = (
            [require_mapping(item, "audit-log event") for item in loaded]
            if isinstance(loaded, list)
            else [require_mapping(loaded, "audit-log event")]
        )
    if len(events) > MAX_RECORDS_PER_INPUT:
        raise AIMeterError("audit-log input contains too many events")
    records = [audit_log_to_record(event) for event in events]
    ensure_unique_record_ids(records)
    return records


def orchestration_record(
    *,
    run_id: str,
    agent_id: str,
    workflow_id: str,
    start_time: datetime,
    end_time: datetime,
    status: str,
    cost: Decimal | None = None,
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
            "source": {"service": "orchestration", "component": "aimeter"},
            "attribution": {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "agent_id": agent_id,
            },
            "model": {"provider": "orchestrator", "requested_model": "agent", "hosting": "unknown"},
            "usage": {"total_tokens": 0},
            "performance": {"latency_ms": int(latency), "status": status},
            "cost": {
                "currency": currency,
                **({"total_cost": money(cost)} if cost is not None else {}),
            },
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
