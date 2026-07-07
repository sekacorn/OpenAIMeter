# OpenAIMeter

OpenAIMeter measures what AI costs, what it accomplishes, and how efficiently it converts compute and model usage into successful outcomes.

Ready for public alpha release with documented limitations.

OpenAIMeter is an alpha, vendor-neutral FinOps and measurement toolkit for AI models, agents, tools, retrieval systems, workflows, and infrastructure. It is local-first and offline-capable. It does not provide invoice accuracy, accounting compliance, automatic savings, production readiness, perfect price data, full OpenTelemetry conformance, or full FOCUS conformance.

## Why

Tokens are not outcomes. A successful API call can still produce a failed business result, and a cheap model can be economically poor if it causes retries, escalations, or human correction. OpenAIMeter stores portable usage records that distinguish measured, provider-reported, user-supplied, calculated, allocated, estimated, and projected values.

OpenAIMeter is project seven in the sekacorn open-source AI infrastructure roadmap and works independently of the other projects.

## Capabilities

- Versioned usage records for model, agent, tool, retrieval, infrastructure, outcome, budget, pricing, and optimization events.
- Decimal cost accounting for provider API usage, local inference estimates, allocations, outcomes, budgets, forecasts, and savings.
- Local JSONL and SQLite storage.
- Cost per successful outcome, provider reports, outcome reports, anomaly reports, CSV export, and FOCUS-inspired export helpers.
- OpenTelemetry-compatible field mapping helpers and W3C trace/span fields in records.
- Adapter protocol for optional integrations without required vendor dependencies.

## Installation

```bash
pip install openaimeter
```

For local development:

```bash
pip install -e .[dev]
```

## Quick Start

```bash
openaimeter validate examples/provider_api/usage.json
openaimeter ingest examples/provider_api/usage.json --database build/meter.db
openaimeter summarize --database build/meter.db
openaimeter report cost-per-success --database build/meter.db
```

## Provider API Example

The provider API example uses fictional, versioned pricing in `examples/provider_api/pricing.yaml`.

```bash
openaimeter pricing calculate examples/provider_api/usage.json --pricing examples/provider_api/pricing.yaml
```

Fictional prices are test fixtures only. Missing prices are reported as unknown, never as zero.

## Local Model Example

The local example uses `qwen2.5:3b` with supplied infrastructure assumptions:

```bash
openaimeter infrastructure calculate examples/local_ollama/usage.json --profile examples/local_ollama/infrastructure.yaml
```

Local inference values are estimates based on supplied assumptions, not invoices.

## Cost Per Success

OpenAIMeter calculates:

```text
total included cost / successful outcome weight
```

Zero attempts and zero successes return explicit statuses rather than dividing by zero.

## Budgets

```bash
openaimeter budget evaluate --database build/meter.db --budget examples/budgets/monthly.yaml
```

OpenAIMeter evaluates budgets and thresholds; it does not enforce spending limits.

## CLI

Common commands:

```bash
openaimeter report cost --database build/meter.db
openaimeter report outcomes --database build/meter.db
openaimeter report providers --database build/meter.db
openaimeter report anomalies --database build/meter.db
openaimeter report reconciliation --database build/meter.db
openaimeter report prometheus --database build/meter.db --output build/metrics.prom
openaimeter report html --database build/meter.db --output build/report.html
openaimeter export --database build/meter.db --format csv --output build/usage.csv
openaimeter pricing sources examples/provider_api/pricing.yaml
openaimeter pricing warnings examples/provider_api/pricing.yaml
openaimeter infrastructure profiles
openaimeter adapters audit-log-ingest examples/audit_log/events.jsonl --database build/audit.db
openaimeter modelswap project examples/modelswap/projection.yaml
openaimeter schema list
openaimeter schema export --output build/schemas
```

## Python API

```python
from pathlib import Path
from open_ai_meter import Meter, load_records

meter = Meter(Path("build/meter.db"))
try:
    for record in load_records(Path("examples/provider_api/usage.json")):
        meter.ingest(record.data)
    print(meter.summarize())
finally:
    meter.close()
```

## Pricing

Pricing tables are local, versioned, and explicit about source references. Rates are stored as currency amount per 1,000,000 tokens. The alpha includes fictional fixture pricing only.

Pricing source management can list source references, source types, verification dates, effective ranges, expiration dates, and stale/expired warnings.

## Infrastructure Allocation

Infrastructure allocation supports weighted allocation with Decimal rounding preservation. Local inference profiles support GPU, CPU, memory, energy, and overhead assumptions.

Allocation helpers support equal share, request count, duration, token count, successful outcome, workflow weight, and custom supplied weights.

## Outcomes

Outcomes include explicit success, score, threshold, quantity, unit, evaluator, evidence reference, correction status, and confidence fields. API success is not treated as business success automatically.

## Reports

Reports are deterministic JSON by default, with CSV export escaping spreadsheet formulas. Prometheus text export and self-contained static HTML reports are available. FOCUS-inspired rows are available through the Python API.

## OpenTelemetry Compatibility

Records include trace and span fields compatible with W3C Trace Context and can be mapped to OpenTelemetry GenAI-style attributes. This project does not claim full OpenTelemetry compliance.

## FOCUS-Inspired Export

The export helper maps OpenAIMeter records to a small FOCUS-inspired cost shape. It is experimental and not a verified FOCUS conformance profile.

## Integrations

Core functionality requires no model provider, cloud SDK, or observability vendor. Optional adapters should implement the `Adapter` protocol and use real public APIs only.

This alpha includes local ecosystem-oriented helpers for audit-log ingestion, orchestration instrumentation records, ModelSwapBench-style replacement projections, budget hook actions, and ontology-based attribution mappings. These helpers are offline and do not claim external integration test results.

## Security

OpenAIMeter uses safe YAML loading, parameterized SQLite statements, CSV formula escaping, metadata depth limits, and local-first storage. Do not store secrets in usage records.

## Limitations

Pricing tables may become stale; calculated cost may differ from invoices; local inference estimates depend on supplied assumptions; infrastructure allocation may be approximate; missing components make totals incomplete; provider token semantics differ; estimated tokens are not provider-reported tokens; outcome quality depends on correct instrumentation; cost per success depends on outcome definitions; projected savings are not realized savings; forecasts are simple deterministic projections; anomaly detection is rule-based; currency conversion requires supplied rates; SQLite is for local use, not a large warehouse; adapters depend on external APIs; OpenTelemetry mapping may be incomplete; FOCUS export is experimental unless formally verified; OpenAIMeter does not enforce budgets itself; schema and APIs may change before 1.0.

## Roadmap

### Completed Alpha Milestones

0.1.0a1 established the offline core: versioned usage records, Decimal cost accounting, provider pricing calculation, local inference estimates, outcomes, cost per success, budgets, forecasts, anomalies, JSONL and SQLite storage, reports, CLI, examples, tests, documentation, and packaging.

0.1.0a2 added richer pricing-source management, price-expiration warnings, more allocation methods, reconciliation reports, Prometheus export, and static HTML reports.

0.1.0a3 added ecosystem adapter helpers, audit-log ingestion, orchestration instrumentation records, local-cost profile templates, ModelSwapBench-style projections, budget hooks, and ontology-based attribution.

### Planned

0.2 will focus on optional PostgreSQL storage, provider invoice imports, signed pricing manifests, organization-level allocation rules, chargeback and showback exports, a dashboard starter, and richer cost-export compatibility.

1.0 will require a stable measurement schema, compatibility policy, verified OpenTelemetry profile, verified cost-export profile, migration policy, security review, and an enterprise-scale storage adapter.

## Contributing

Contributions should keep the core offline-capable, deterministic, typed, and explicit about data provenance.

## License

MIT

Author: sekacorn
