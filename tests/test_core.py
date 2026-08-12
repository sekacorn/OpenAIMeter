from __future__ import annotations

import json
import sys
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest

from ai_meter.core import (
    AIMeterError,
    AllocationInput,
    Budget,
    InfrastructureProfile,
    JsonlStore,
    Meter,
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
    load_audit_log,
    load_records,
    load_single_record,
    load_yaml,
    replacement_savings,
    report,
    safe_csv_cell,
    success_rate,
    summarize_costs,
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
    with pytest.raises(AIMeterError, match=message):
        load_records(ROOT / path)


def test_inconsistent_token_total_rejected() -> None:
    raw = provider_record()
    raw["usage"]["total_tokens"] = 1
    with pytest.raises(AIMeterError, match="inconsistent"):
        validate_record(raw)


def test_invalid_timestamp_and_nonfinite_decimal_rejected() -> None:
    raw = provider_record()
    raw["start_time"] = "not-a-time"
    with pytest.raises(AIMeterError, match="invalid timestamp"):
        validate_record(raw)

    raw = provider_record()
    raw["cost"]["total_cost"] = "NaN"
    with pytest.raises(AIMeterError, match="finite Decimal"):
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


def test_missing_active_pricing_rate_is_unknown_not_zero() -> None:
    record = validate_record(provider_record())
    entry = dict(PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml").entries[0])
    del entry["output_token_price"]
    result = calculate_provider_cost(record, PricingTable(version="x", entries=[entry]))
    assert result["status"] == "unknown"
    assert result["resolution"] == "missing_rate"
    assert result["missing_rates"] == ["output_token_price"]


def test_ambiguous_pricing() -> None:
    entry = PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml").entries[0]
    with pytest.raises(AIMeterError, match="overlapping"):
        PricingTable(version="x", entries=[entry, dict(entry)])


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
    with pytest.raises(AIMeterError, match="missing"):
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
        provider = report(records, "providers")["providers"]["example-provider"]["USD"]
        assert provider["total_cost"] == "0.003040"
        assert provider["status"] == "complete"
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
    with pytest.raises(AIMeterError, match="duplicate"):
        store.append(record)


def test_csv_formula_escaping_and_savings() -> None:
    assert safe_csv_cell("=1+1") == "'=1+1"
    assert safe_csv_cell("  =1+1") == "'  =1+1"
    assert (
        replacement_savings(Decimal("2"), Decimal("1"), realized=False)["category"]
        == "benchmark_projected"
    )
    assert cache_avoided_cost(Decimal("2"), Decimal("1"))["category"] == "estimated"


def test_cache_hit_rate() -> None:
    records = load_records(ROOT / "examples/caching/usage.json")
    assert cache_hit_rate(records) == Decimal("1")


def test_json_shape_validation(tmp_path: Path) -> None:
    scalar = tmp_path / "scalar.json"
    scalar.write_text('"not-a-record"', encoding="utf-8")
    with pytest.raises(AIMeterError, match="expected JSON object or array"):
        load_records(scalar)

    jsonl = tmp_path / "bad.jsonl"
    jsonl.write_text("[]\n", encoding="utf-8")
    with pytest.raises(AIMeterError, match="JSONL record must be a JSON object"):
        load_records(jsonl)


def test_unknown_cost_is_never_aggregated_as_zero() -> None:
    raw = provider_record()
    del raw["cost"]["total_cost"]
    record = validate_record(raw)
    assert record.total_cost is None
    summary = summarize_costs([record])
    assert summary.status == "incomplete_cost_data"
    assert summary.total_cost is None
    assert summary.known_total_cost == Decimal("0")
    result = cost_per_success([record])
    assert result.status == "incomplete_cost_data"
    assert result.value is None
    budget = Budget("b", Decimal("1"), "USD", Decimal("80"), Decimal("100"))
    assert evaluate_budget([record], budget)["status"] == "incomplete_cost_data"


def test_unknown_outcome_is_not_a_failed_outcome() -> None:
    raw = provider_record()
    raw["outcome"] = {}
    record = validate_record(raw)
    assert record.success_weight is None
    assert success_rate([record]) is None
    assert cost_per_success([record]).status == "incomplete_outcome_data"
    assert report([record], "outcomes")["status"] == "incomplete_outcome_data"


def test_provider_report_keeps_currencies_separate() -> None:
    usd = validate_record(provider_record())
    eur_data = provider_record()
    eur_data["record_id"] = "provider-api-eur"
    eur_data["cost"]["currency"] = "EUR"
    eur_data["cost"]["total_cost"] = "0.004000"
    eur = validate_record(eur_data)

    providers = report([usd, eur], "providers")["providers"]["example-provider"]
    assert providers["USD"]["total_cost"] == "0.003040"
    assert providers["EUR"]["total_cost"] == "0.004000"


def test_validation_does_not_mutate_input_or_generate_ids() -> None:
    raw = provider_record()
    original = json.loads(json.dumps(raw))
    validate_record(raw)
    assert raw == original
    del raw["record_id"]
    with pytest.raises(AIMeterError, match="record_id is required"):
        validate_record(raw)


def test_pricing_validity_and_missing_usage_are_unknown() -> None:
    entry = dict(PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml").entries[0])
    entry["effective_end"] = "2026-07-02T00:00:00Z"
    table = PricingTable(version="x", entries=[entry])
    result = calculate_provider_cost(validate_record(provider_record()), table)
    assert result["status"] == "unknown"
    assert result["resolution"] == "outside_validity_period"

    raw = provider_record()
    del raw["usage"]["output_tokens"]
    result = calculate_provider_cost(
        validate_record(raw), PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml")
    )
    assert result["status"] == "unknown"
    assert result["resolution"] == "missing_usage"

    calculated = calculate_provider_cost(
        validate_record(provider_record()),
        PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml"),
    )
    assert calculated["pricing_provenance"]["source_type"] == "test_fixture"


def test_duplicate_keys_and_duplicate_record_ids_are_rejected(tmp_path: Path) -> None:
    duplicate_key = tmp_path / "duplicate.json"
    duplicate_key.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    with pytest.raises(AIMeterError, match="duplicate JSON key"):
        load_records(duplicate_key)

    first = provider_record()
    duplicate_records = tmp_path / "duplicate-records.json"
    duplicate_records.write_text(json.dumps([first, first]), encoding="utf-8")
    with pytest.raises(AIMeterError, match="duplicate record_id"):
        load_records(duplicate_records)

    duplicate_yaml = tmp_path / "duplicate.yaml"
    duplicate_yaml.write_text("version: one\nversion: two\nentries: []\n", encoding="utf-8")
    with pytest.raises(AIMeterError, match="duplicate YAML key"):
        PricingTable.from_file(duplicate_yaml)

    unsupported_yaml_key = tmp_path / "unsupported-key.yaml"
    unsupported_yaml_key.write_text("? [not, scalar]\n: value\n", encoding="utf-8")
    with pytest.raises(AIMeterError, match="keys must be scalar"):
        load_yaml(unsupported_yaml_key)


def test_single_record_calculators_reject_empty_or_multiple_input(tmp_path: Path) -> None:
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps([provider_record(), {**provider_record(), "record_id": "two"}]), encoding="utf-8"
    )
    with pytest.raises(AIMeterError, match="exactly one"):
        load_single_record(path)


def test_sqlite_batch_is_atomic_and_preserves_unknown_costs(tmp_path: Path) -> None:
    db = tmp_path / "meter.db"
    record = validate_record(provider_record())
    store = SQLiteStore(db)
    try:
        with pytest.raises(AIMeterError, match="duplicate record_id"):
            store.add_many([record, record])
        assert store.all() == []

        unknown = provider_record()
        unknown["record_id"] = "unknown-cost"
        del unknown["cost"]["total_cost"]
        store.add(validate_record(unknown))
        assert store.all()[0].total_cost is None
    finally:
        store.close()


def test_audit_log_adapter_preserves_absent_cost_as_unknown(tmp_path: Path) -> None:
    source = tmp_path / "events.jsonl"
    source.write_text(
        json.dumps(
            {
                "event_id": "event-without-cost",
                "time": "2026-07-06T15:00:00Z",
                "provider": "example-provider",
                "model": "fictional-fast-1",
                "usage": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert load_audit_log(source)[0].total_cost is None


def test_deprecated_import_shim_warns() -> None:
    sys.modules.pop("open_ai_meter", None)
    with pytest.warns(DeprecationWarning, match="open_ai_meter has been renamed to ai_meter"):
        module = import_module("open_ai_meter")
    assert module.__version__ == "0.2.0b1"
