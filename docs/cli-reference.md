# CLI Reference

Commands include `validate`, `ingest`, `summarize`, `report`, `pricing`, `infrastructure`, `budget`, `adapters`, `modelswap`, `export`, and `schema`.

Additional report formats:

- `openaimeter report reconciliation --database build/meter.db`
- `openaimeter report prometheus --database build/meter.db --output build/metrics.prom`
- `openaimeter report html --database build/meter.db --output build/report.html`

Pricing source management:

- `openaimeter pricing sources examples/provider_api/pricing.yaml`
- `openaimeter pricing warnings examples/provider_api/pricing.yaml`

Ecosystem helpers:

- `openaimeter adapters audit-log-ingest examples/audit_log/events.jsonl --database build/audit.db`
- `openaimeter modelswap project examples/modelswap/projection.yaml`
