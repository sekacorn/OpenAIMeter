from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from open_ai_meter.core import (
    AllocationInput,
    Budget,
    InfrastructureProfile,
    JsonlStore,
    Meter,
    OpenAIMeterError,
    PricingTable,
    SQLiteStore,
    allocate_costs,
    cache_avoided_cost,
    cache_hit_rate,
    calculate_local_inference_cost,
    calculate_provider_cost,
    cost_per_success,
    detect_anomalies,
    evaluate_budget,
    export_csv,
    export_focus_rows,
    forecast_spend,
    load_records,
    replacement_savings,
    report,
    safe_csv_cell,
    success_rate,
    validate_record,
)

ROOT = Path(__file__).resolve().parents[1]


def provider_record() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / "examples/provider_api/usage.json").read_text()))


def test_validate_record_and_decimal_serialization() -> None:
    record = validate_record(provider_record())
    assert record.record_id == "provider-api-001"
    assert record.total_cost == Decimal("0.003040")
    assert "provider-api-001" in record.to_json()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("examples/invalid/negative_tokens.json", "nonnegative"),
        ("examples/invalid/unknown_currency.json", "unsupported currency"),
    ],
)
def test_invalid_fixtures(path: str, message: str) -> None:
    with pytest.raises(OpenAIMeterError, match=message):
        load_records(ROOT / path)


def test_inconsistent_token_total_rejected() -> None:
    raw = provider_record()
    raw["usage"]["total_tokens"] = 1
    with pytest.raises(OpenAIMeterError, match="inconsistent"):
        validate_record(raw)


def test_pricing_calculation_exact() -> None:
    record = validate_record(provider_record())
    table = PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml")
    result = calculate_provider_cost(record, table)
    assert result["status"] == "calculated"
    assert result["resolution"] == "exact"
    assert result["provider_cost"] == "0.003040"


def test_missing_price_is_unknown_not_zero() -> None:
    record = validate_record(provider_record())
    table = PricingTable(version="x", entries=[])
    result = calculate_provider_cost(record, table)
    assert result["status"] == "unknown"
    assert result["provider_cost"] is None


def test_ambiguous_pricing() -> None:
    entry = PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml").entries[0]
    table = PricingTable(version="x", entries=[entry, dict(entry)])
    result = calculate_provider_cost(validate_record(provider_record()), table)
    assert result["resolution"] == "ambiguous"


def test_provider_reported_cost_kept_separate() -> None:
    raw = provider_record()
    raw["cost"]["provider_reported_cost"] = "0.0042"
    result = calculate_provider_cost(validate_record(raw), PricingTable(version="x", entries=[]))
    assert result["status"] == "provider_reported"
    assert result["provider_cost"] == "0.004200"


def test_local_inference_cost() -> None:
    record = load_records(ROOT / "examples/local_ollama/usage.json")[0]
    profile = InfrastructureProfile.from_file(ROOT / "examples/local_ollama/infrastructure.yaml")
    result = calculate_local_inference_cost(record, profile)
    assert result["status"] == "estimated"
    assert Decimal(result["total_cost"]) > Decimal("0")


def test_local_inference_missing_assumptions() -> None:
    record = load_records(ROOT / "examples/local_ollama/usage.json")[0]
    with pytest.raises(OpenAIMeterError, match="missing"):
        calculate_local_inference_cost(
            record, InfrastructureProfile({"profile": {"currency": "USD", "compute": {}}})
        )


def test_allocation_preserves_total() -> None:
    allocated = allocate_costs(
        AllocationInput(Decimal("1.000000"), [Decimal("1"), Decimal("1"), Decimal("1")])
    )
    assert sum(allocated, Decimal("0")) == Decimal("1.000000")
    assert allocated[-1] == Decimal("0.333334")


def test_cost_per_success_and_zero_success() -> None:
    record = validate_record(provider_record())
    result = cost_per_success([record])
    assert result.status == "defined"
    assert result.value == Decimal("0.003040")
    raw = provider_record()
    raw["outcome"]["success"] = False
    zero = cost_per_success([validate_record(raw)])
    assert zero.status == "zero_successes"
    assert zero.value is None


def test_budget_forecast_anomalies_and_rates() -> None:
    records = [validate_record(provider_record())]
    budget = Budget(
        id="b",
        amount=Decimal("0.001"),
        currency="USD",
        warning_threshold=Decimal("80"),
        critical_threshold=Decimal("100"),
    )
    assert evaluate_budget(records, budget)["status"] == "exceeded"
    assert forecast_spend(records)["status"] == "insufficient_samples"
    raw2 = provider_record()
    raw2["record_id"] = "r2"
    raw2["start_time"] = "2026-07-07T12:30:45Z"
    raw2["end_time"] = "2026-07-07T12:30:46Z"
    assert forecast_spend([records[0], validate_record(raw2)])["status"] == "forecast"
    assert success_rate(records) == Decimal("1")
    assert detect_anomalies(records) == []


def test_storage_reports_and_exports(tmp_path: Path) -> None:
    db = tmp_path / "meter.db"
    meter = Meter(db)
    try:
        meter.ingest(provider_record())
        assert meter.summarize()["records"] == 1
    finally:
        meter.close()
    store = SQLiteStore(db)
    try:
        records = store.all()
        assert report(records, "cost-per-success")["status"] == "defined"
        assert report(records, "providers")["providers"]["example-provider"] == "0.003040"
        csv_path = tmp_path / "usage.csv"
        export_csv(records, csv_path)
        assert "provider-api-001" in csv_path.read_text()
        assert export_focus_rows(records)[0]["ProviderName"] == "example-provider"
    finally:
        store.close()


def test_jsonl_duplicate_rejected(tmp_path: Path) -> None:
    store = JsonlStore(tmp_path / "records.jsonl")
    record = validate_record(provider_record())
    store.append(record)
    with pytest.raises(OpenAIMeterError, match="duplicate"):
        store.append(record)


def test_csv_formula_escaping_and_savings() -> None:
    assert safe_csv_cell("=1+1") == "'=1+1"
    assert (
        replacement_savings(Decimal("2"), Decimal("1"), realized=False)["category"]
        == "benchmark_projected"
    )
    assert cache_avoided_cost(Decimal("2"), Decimal("1"))["category"] == "estimated"


def test_cache_hit_rate() -> None:
    records = load_records(ROOT / "examples/caching/usage.json")
    assert cache_hit_rate(records) == Decimal("1")
