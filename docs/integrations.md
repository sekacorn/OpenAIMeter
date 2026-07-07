# Integrations

The core has an `Adapter` protocol for optional integrations. Adapters must use real public APIs and must not fabricate integration results.

Implemented offline helpers include audit-log event ingestion, orchestration instrumentation records, ModelSwapBench-style replacement projections, budget hook actions, and ontology-based attribution mappings. These are local transformations and do not claim external service verification.
