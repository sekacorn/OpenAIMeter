# AIMeter

AIMeter measures what AI costs, what it accomplishes, and how efficiently it converts compute and model usage into successful outcomes.

Current release candidate: `0.2.0b1` public Beta software with documented limitations. It is not yet tagged or published.

AIMeter is a Beta, vendor-neutral FinOps and measurement toolkit for AI models, agents, tools, retrieval systems, workflows, and infrastructure. It is local-first and offline-capable. It does not provide invoice accuracy, accounting compliance, automatic savings, production readiness, perfect price data, full OpenTelemetry conformance, or full FOCUS conformance.

## Why

Tokens are not outcomes. A successful API call can still produce a failed business result, and a cheap model can be economically poor if it causes retries, escalations, or human correction. AIMeter stores portable usage records that distinguish measured, provider-reported, user-supplied, calculated, allocated, estimated, and projected values.

AIMeter is project seven in the sekacorn open-source AI infrastructure roadmap and works independently of the other projects.

## Migration Note

This project was previously published as openaimeter / OpenAIMeter. It has been renamed to aimeter-oss / AIMeter to avoid confusion with any AI provider brand and to better reflect its vendor-neutral purpose.

## Capabilities

- Versioned usage records for model, agent, tool, retrieval, infrastructure, outcome, budget, pricing, and optimization events.
- Decimal cost accounting for provider API usage, local inference estimates, allocations, outcomes, budgets, forecasts, and savings.
- Completeness-aware aggregates: an absent total cost or outcome stays unknown and cannot become a complete total, budget result, forecast, or cost-per-success claim.
- Local JSONL and SQLite storage.
- Cost per successful outcome, provider reports, outcome reports, anomaly reports, CSV export, and FOCUS-inspired export helpers.
- OpenTelemetry-compatible field mapping helpers and W3C trace/span fields in records.
- Adapter protocol for optional integrations without required vendor dependencies.

## Installation

```bash
pip install aimeter-oss
```

For local development:

```bash
pip install -e .[dev]
```

## Quick Start

```bash
aimeter validate examples/provider_api/usage.json
aimeter ingest examples/provider_api/usage.json --database build/meter.db
aimeter summarize --database build/meter.db
aimeter report cost-per-success --database build/meter.db
```

## Provider API Example

The provider API example uses fictional, versioned pricing in `examples/provider_api/pricing.yaml`.

```bash
aimeter pricing calculate examples/provider_api/usage.json --pricing examples/provider_api/pricing.yaml
```

Fictional prices are test fixtures only. Missing prices, out-of-window prices, missing billable usage, and malformed or ambiguous pricing are reported as unknown, never as zero. Pricing entries are local, versioned, time-bounded, and include a source reference; calculated list-price arithmetic is not proof of an invoice amount or negotiated enterprise rate.

## Local Model Example

The local example uses `qwen2.5:3b` with supplied infrastructure assumptions:

```bash
aimeter infrastructure calculate examples/local_ollama/usage.json --profile examples/local_ollama/infrastructure.yaml
```

Local inference values are estimates based on supplied assumptions, not invoices.

## Cost Per Success

AIMeter calculates:

```text
total included cost / successful outcome weight
```

Zero attempts and zero successes return explicit statuses rather than dividing by zero. If any included cost or outcome is unknown, cost per success is marked incomplete and has no value. API success is not a business outcome unless an outcome is explicitly recorded.

## Budgets

```bash
aimeter budget evaluate --database build/meter.db --budget examples/budgets/monthly.yaml
```

AIMeter evaluates budgets and thresholds; it does not enforce spending limits.

## CLI

Common commands:

```bash
aimeter report cost --database build/meter.db
aimeter report outcomes --database build/meter.db
aimeter report providers --database build/meter.db
aimeter report anomalies --database build/meter.db
aimeter report reconciliation --database build/meter.db
aimeter report prometheus --database build/meter.db --output build/metrics.prom
aimeter report html --database build/meter.db --output build/report.html
aimeter export --database build/meter.db --format csv --output build/usage.csv
aimeter pricing sources examples/provider_api/pricing.yaml
aimeter pricing warnings examples/provider_api/pricing.yaml
aimeter infrastructure profiles
aimeter adapters audit-log-ingest examples/audit_log/events.jsonl --database build/audit.db
aimeter modelswap project examples/modelswap/projection.yaml
aimeter schema list
aimeter schema export --output build/schemas
```

## Python API

```python
from pathlib import Path
from ai_meter import Meter, load_records

meter = Meter(Path("build/meter.db"))
try:
    for record in load_records(Path("examples/provider_api/usage.json")):
        meter.ingest(record.data)
    print(meter.summarize())
finally:
    meter.close()
```

## Pricing

Pricing tables are local, versioned, and explicit about source references. Rates are stored as currency amount per 1,000,000 tokens. Resolution requires a matching provider, model key, region, currency, and effective time window. Overlapping windows and duplicate YAML keys are rejected. The Beta includes fictional fixture pricing only.

Pricing source management can list source references, source types, verification dates, effective ranges, expiration dates, and stale/expired warnings.

## Infrastructure Allocation

Infrastructure allocation supports weighted allocation with Decimal rounding preservation. Local inference profiles support GPU, CPU, memory, energy, and overhead assumptions.

Allocation helpers support equal share, request count, duration, token count, successful outcome, workflow weight, and custom supplied weights.

## Outcomes

Outcomes include explicit success, score, threshold, quantity, unit, evaluator, evidence reference, correction status, and confidence fields. API success is not treated as business success automatically.

## Reports

Reports are deterministic JSON by default, with CSV export escaping spreadsheet formulas. Cost reports include a completeness status, known subtotal, and unknown-record count; a complete total is emitted only when every included cost is known. Provider reports keep subtotals separate by currency. Prometheus text export and self-contained static HTML reports are available. FOCUS-inspired rows are available through the Python API.

## OpenTelemetry Compatibility

Records include trace and span fields compatible with W3C Trace Context and can be mapped to OpenTelemetry GenAI-style attributes. This project does not claim full OpenTelemetry compliance.

## FOCUS-Inspired Export

The export helper maps AIMeter records to a small FOCUS-inspired cost shape. It is experimental and not a verified FOCUS conformance profile.

## Integrations

Core functionality requires no model provider, cloud SDK, or observability vendor. Optional adapters should implement the `Adapter` protocol and use real public APIs only.

This Beta includes local ecosystem-oriented helpers for audit-log ingestion, orchestration instrumentation records, ModelSwapBench-style replacement projections, budget hook actions, and ontology-based attribution mappings. These helpers are offline and do not claim external integration test results.

## Security

AIMeter uses bounded UTF-8 JSON/JSONL/YAML loading, duplicate-key rejection, duplicate record-ID rejection, parameterized SQLite statements, CSV formula escaping, HTML escaping, metadata and structural depth limits, and local-first storage. SQLite batch ingestion is transactional. Do not store secrets in usage records.

## Determinism and Storage

Records require a stable `record_id`; validation does not generate one or mutate the caller's input. JSON/JSONL imports reject duplicate IDs before aggregation, and SQLite rejects duplicate IDs atomically. JSONL is append-only local storage; SQLite is a local convenience index, not an accounting ledger or enterprise warehouse. Equivalent valid records and pricing tables produce deterministic ordering and six-decimal monetary serialization.

## Limitations

Pricing tables may become stale; calculated cost may differ from invoices; local inference estimates depend on supplied assumptions; infrastructure allocation may be approximate; missing components make totals incomplete; provider token semantics differ; estimated tokens are not provider-reported tokens; outcome quality depends on correct instrumentation; cost per success depends on outcome definitions; projected savings are not realized savings; forecasts are simple deterministic projections; anomaly detection is rule-based; currency conversion requires supplied rates; SQLite is for local use, not a large warehouse; adapters depend on external APIs; OpenTelemetry mapping may be incomplete; FOCUS export is experimental unless formally verified; AIMeter does not enforce budgets itself; schema and APIs may change before 1.0.

## Roadmap

### Completed Alpha Milestones

0.1.0a1 established the offline core: versioned usage records, Decimal cost accounting, provider pricing calculation, local inference estimates, outcomes, cost per success, budgets, forecasts, anomalies, JSONL and SQLite storage, reports, CLI, examples, tests, documentation, and packaging.

0.1.0a2 added richer pricing-source management, price-expiration warnings, more allocation methods, reconciliation reports, Prometheus export, and static HTML reports.

0.1.0a3 added ecosystem adapter helpers, audit-log ingestion, orchestration instrumentation records, local-cost profile templates, ModelSwapBench-style projections, budget hooks, and ontology-based attribution.

0.1.0a4 renamed the project to AIMeter, the distribution to `aimeter-oss`, the import package to `ai_meter`, and the CLI command to `aimeter`. Deprecated compatibility shims are included for the previous import and CLI names during the transition.

0.1.0a5 published corrected Apache-2.0 metadata under the public maintainer identity `sekacorn`; runtime behavior was unchanged from 0.1.0a4.

### Planned

The current pre-Beta hardening work makes unknown cost and outcome states explicit, rejects ambiguous pricing and duplicate input, and preserves deterministic local ingestion. Future work may include optional PostgreSQL storage, provider invoice imports, signed pricing manifests, organization-level allocation rules, chargeback and showback exports, a dashboard starter, and richer cost-export compatibility.

1.0 will require a stable measurement schema, compatibility policy, verified OpenTelemetry profile, verified cost-export profile, migration policy, security review, and an enterprise-scale storage adapter.

## Contributing

Contributions should keep the core offline-capable, deterministic, typed, and explicit about data provenance.

## License

[Apache License 2.0](LICENSE)

Author: sekacorn
