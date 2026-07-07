from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from open_ai_meter.core import (
    Budget,
    PricingTable,
    UsageRecord,
    allocate_records,
    apply_ontology_attribution,
    budget_hooks,
    export_prometheus_metrics,
    load_audit_log,
    load_records,
    load_yaml,
    local_cost_profile_catalog,
    model_swap_projection,
    orchestration_record,
    pricing_source_warnings,
    reconcile_costs,
    render_static_html_report,
    validate_record,
)

ROOT = Path(__file__).resolve().parents[1]


def record_pair() -> list[UsageRecord]:
    first = load_records(ROOT / "examples/provider_api/usage.json")[0]
    second_data = json.loads(first.to_json())
    second_data["record_id"] = "provider-api-002"
    second_data["start_time"] = "2026-07-06T12:31:45Z"
    second_data["end_time"] = "2026-07-06T12:31:46Z"
    second_data["usage"]["total_tokens"] = 100
    second_data["usage"]["input_tokens"] = 50
    second_data["usage"]["output_tokens"] = 50
    second_data["cost"]["total_cost"] = "0.001000"
    return [first, validate_record(second_data)]


def test_pricing_sources_and_expiration_warnings() -> None:
    table = PricingTable.from_file(ROOT / "examples/provider_api/pricing.yaml")
    assert table.sources()[0]["source_type"] == "test_fixture"
    warnings = pricing_source_warnings(
        table, as_of=datetime(2026, 8, 2, tzinfo=UTC), stale_after_days=1
    )
    reasons = {warning["reason"] for warning in warnings}
    assert "pricing_expired" in reasons
    assert "stale_pricing_source" in reasons


def test_more_allocation_methods() -> None:
    records = record_pair()
    equal = allocate_records(Decimal("1.000000"), records, "equal_share")
    tokens = allocate_records(Decimal("1.000000"), records, "token_count")
    duration = allocate_records(Decimal("1.000000"), records, "duration")
    assert equal[0]["allocated_amount"] == "0.500000"
    assert Decimal(tokens[0]["allocated_amount"]) > Decimal(tokens[1]["allocated_amount"])
    assert sum(Decimal(item["allocated_amount"]) for item in duration) == Decimal("1.000000")


def test_reconciliation_prometheus_and_html() -> None:
    raw = json.loads((ROOT / "examples/provider_api/usage.json").read_text())
    raw["cost"]["provider_reported_cost"] = "0.003040"
    raw["cost"]["invoiced_cost"] = "0.003041"
    record = validate_record(raw)
    reconciliation = reconcile_costs([record], tolerance=Decimal("0.000001"))
    assert reconciliation[0]["status"] == "matched"
    prom = export_prometheus_metrics([record])
    assert "openaimeter_cost_total" in prom
    html = render_static_html_report([record])
    assert "<table>" in html
    assert "provider-api-001" in html


def test_audit_log_ingestion_and_orchestration_record() -> None:
    imported = load_audit_log(ROOT / "examples/audit_log/events.jsonl")
    assert imported[0].record_id == "audit-001"
    start = datetime(2026, 7, 6, 15, 1, tzinfo=UTC)
    record = orchestration_record(
        run_id="run-42",
        agent_id="agent-42",
        workflow_id="claim-review",
        start_time=start,
        end_time=start + timedelta(seconds=2),
        status="success",
        cost=Decimal("0.001"),
    )
    assert record.data["record_type"] == "agent.run"
    assert record.success_weight == Decimal("1")


def test_local_profiles_modelswap_budget_hooks_and_ontology() -> None:
    assert "local-rtx-workstation" in local_cost_profile_catalog()
    projection = model_swap_projection(load_yaml(ROOT / "examples/modelswap/projection.yaml"))
    assert projection["category"] == "benchmark_projected"
    assert projection["realized"] is False
    assert projection["projected_savings"] == "7.500000"
    records = load_records(ROOT / "examples/provider_api/usage.json")
    budget = Budget(
        id="tiny",
        amount=Decimal("0.001"),
        currency="USD",
        warning_threshold=Decimal("80"),
        critical_threshold=Decimal("100"),
    )
    assert budget_hooks(records, budget)[0]["action"] == "block_nonessential"
    ontology = load_yaml(ROOT / "examples/ontology/attribution.yaml")
    attributed = apply_ontology_attribution(records[0], ontology)
    assert attributed.data["attribution"]["business_capability"] == "claims_adjudication"
