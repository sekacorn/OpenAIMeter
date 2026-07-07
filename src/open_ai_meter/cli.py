"""Command line interface for OpenAIMeter."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from open_ai_meter import __version__
from open_ai_meter.core import (
    Budget,
    InfrastructureProfile,
    Meter,
    OpenAIMeterError,
    PricingTable,
    SQLiteStore,
    calculate_local_inference_cost,
    calculate_provider_cost,
    export_csv,
    export_prometheus_metrics,
    load_audit_log,
    load_records,
    load_yaml,
    local_cost_profile_catalog,
    model_swap_projection,
    pricing_source_warnings,
    reconcile_costs,
    render_static_html_report,
    report,
    write_json,
)


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openaimeter")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("input")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("input")
    ingest.add_argument("--database", required=True)

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--database", required=True)

    rep = sub.add_parser("report")
    rep_sub = rep.add_subparsers(dest="kind", required=True)
    for kind in (
        "cost",
        "outcomes",
        "cost-per-success",
        "providers",
        "anomalies",
        "prometheus",
        "html",
        "reconciliation",
    ):
        child = rep_sub.add_parser(kind)
        child.add_argument("--database", required=True)
        child.add_argument("--output")

    pricing = sub.add_parser("pricing")
    pricing_sub = pricing.add_subparsers(dest="pricing_command", required=True)
    pricing_validate = pricing_sub.add_parser("validate")
    pricing_validate.add_argument("pricing")
    pricing_sources = pricing_sub.add_parser("sources")
    pricing_sources.add_argument("pricing")
    pricing_warnings = pricing_sub.add_parser("warnings")
    pricing_warnings.add_argument("pricing")
    pricing_warnings.add_argument("--stale-after-days", type=int, default=90)
    pricing_calc = pricing_sub.add_parser("calculate")
    pricing_calc.add_argument("input")
    pricing_calc.add_argument("--pricing", required=True)

    infra = sub.add_parser("infrastructure")
    infra_sub = infra.add_subparsers(dest="infra_command", required=True)
    infra_calc = infra_sub.add_parser("calculate")
    infra_calc.add_argument("input")
    infra_calc.add_argument("--profile", required=True)
    infra_profiles = infra_sub.add_parser("profiles")
    infra_profiles.add_argument("--output")

    adapters = sub.add_parser("adapters")
    adapters_sub = adapters.add_subparsers(dest="adapter_command", required=True)
    audit_ingest = adapters_sub.add_parser("audit-log-ingest")
    audit_ingest.add_argument("input")
    audit_ingest.add_argument("--database", required=True)

    model_swap = sub.add_parser("modelswap")
    model_swap_sub = model_swap.add_subparsers(dest="modelswap_command", required=True)
    model_swap_project = model_swap_sub.add_parser("project")
    model_swap_project.add_argument("input")

    budget = sub.add_parser("budget")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)
    budget_eval = budget_sub.add_parser("evaluate")
    budget_eval.add_argument("--database", required=True)
    budget_eval.add_argument("--budget", required=True)

    export = sub.add_parser("export")
    export.add_argument("--database", required=True)
    export.add_argument("--format", choices=["csv", "json"], required=True)
    export.add_argument("--output", required=True)

    schema = sub.add_parser("schema")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_sub.add_parser("list")
    schema_export = schema_sub.add_parser("export")
    schema_export.add_argument("--output", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            records = load_records(Path(args.input))
            print_json({"status": "valid", "records": len(records)})
            return 0
        if args.command == "ingest":
            meter = Meter(Path(args.database))
            try:
                count = 0
                for record in load_records(Path(args.input)):
                    meter.ingest(record.data)
                    count += 1
                print_json({"status": "ingested", "records": count})
            finally:
                meter.close()
            return 0
        if args.command == "summarize":
            store = SQLiteStore(Path(args.database))
            try:
                print_json(store.summarize())
            finally:
                store.close()
            return 0
        if args.command == "report":
            store = SQLiteStore(Path(args.database))
            try:
                records = store.all()
                if args.kind == "prometheus":
                    text = export_prometheus_metrics(records)
                    if args.output:
                        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                        Path(args.output).write_text(text, encoding="utf-8")
                    else:
                        print(text, end="")
                    return 0
                if args.kind == "html":
                    text = render_static_html_report(records)
                    if args.output:
                        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                        Path(args.output).write_text(text, encoding="utf-8")
                    else:
                        print(text)
                    return 0
                if args.kind == "reconciliation":
                    print_json({"reconciliation": reconcile_costs(records)})
                    return 0
                print_json(report(records, args.kind))
            finally:
                store.close()
            return 0
        if args.command == "pricing":
            table = PricingTable.from_file(Path(args.pricing))
            if args.pricing_command == "validate":
                print_json(
                    {"status": "valid", "entries": len(table.entries), "version": table.version}
                )
                return 0
            if args.pricing_command == "sources":
                print_json({"version": table.version, "sources": table.sources()})
                return 0
            if args.pricing_command == "warnings":
                print_json(
                    {
                        "version": table.version,
                        "warnings": pricing_source_warnings(
                            table, stale_after_days=args.stale_after_days
                        ),
                    }
                )
                return 0
            record = load_records(Path(args.input))[0]
            print_json(calculate_provider_cost(record, table))
            return 0
        if args.command == "infrastructure":
            if args.infra_command == "profiles":
                profiles = local_cost_profile_catalog()
                if args.output:
                    write_json(Path(args.output), profiles)
                else:
                    print_json(profiles)
                return 0
            record = load_records(Path(args.input))[0]
            profile = InfrastructureProfile.from_file(Path(args.profile))
            print_json(calculate_local_inference_cost(record, profile))
            return 0
        if args.command == "adapters" and args.adapter_command == "audit-log-ingest":
            records = load_audit_log(Path(args.input))
            meter = Meter(Path(args.database))
            try:
                for record in records:
                    meter.ingest(record.data)
            finally:
                meter.close()
            print_json({"status": "ingested", "records": len(records), "adapter": "audit-log"})
            return 0
        if args.command == "modelswap" and args.modelswap_command == "project":
            print_json(model_swap_projection(load_yaml(Path(args.input))))
            return 0
        if args.command == "budget":
            store = SQLiteStore(Path(args.database))
            try:
                from open_ai_meter.core import evaluate_budget

                print_json(evaluate_budget(store.all(), Budget.from_file(Path(args.budget))))
            finally:
                store.close()
            return 0
        if args.command == "export":
            store = SQLiteStore(Path(args.database))
            try:
                records = store.all()
                output = Path(args.output)
                if args.format == "csv":
                    export_csv(records, output)
                else:
                    write_json(output, [record.data for record in records])
                print_json({"status": "exported", "output": str(output)})
            finally:
                store.close()
            return 0
        if args.command == "schema":
            schema_dir = Path(__file__).parent / "schemas"
            if args.schema_command == "list":
                print_json({"schemas": ["usage-record-v1.schema.json"]})
                return 0
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                schema_dir / "usage-record-v1.schema.json", output / "usage-record-v1.schema.json"
            )
            print_json({"status": "exported", "output": str(output)})
            return 0
    except (OpenAIMeterError, KeyError, json.JSONDecodeError) as exc:
        print(f"openaimeter: error: {exc}", file=sys.stderr)
        return 2
    parser.error("unhandled command")
    return 2


def app() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    app()
