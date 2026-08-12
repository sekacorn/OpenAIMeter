# CLI Reference

Commands include `validate`, `ingest`, `summarize`, `report`, `pricing`, `infrastructure`, `budget`, `adapters`, `modelswap`, `export`, and `schema`.

`pricing calculate` and `infrastructure calculate` require exactly one record. Bulk `ingest` validates the full input and rejects duplicate record IDs before writing to SQLite. A cost, provider, budget, forecast, or cost-per-success result with `incomplete_cost_data` has a known subtotal where applicable, but no complete monetary claim.

Additional report formats:

- `aimeter report reconciliation --database build/meter.db`
- `aimeter report prometheus --database build/meter.db --output build/metrics.prom`
- `aimeter report html --database build/meter.db --output build/report.html`

Pricing source management:

- `aimeter pricing sources examples/provider_api/pricing.yaml`
- `aimeter pricing warnings examples/provider_api/pricing.yaml`

Ecosystem helpers:

- `aimeter adapters audit-log-ingest examples/audit_log/events.jsonl --database build/audit.db`
- `aimeter modelswap project examples/modelswap/projection.yaml`
