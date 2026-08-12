# Measurement Model

AIMeter records use schema version `1.0` and store source, attribution, model identity, usage, performance, cost, outcome, correlation, and metadata fields. Quantitative values must be interpreted with provenance and calculation method fields.

Required fields are `schema_version`, `record_id`, `record_type`, `start_time`, `end_time`, `model`, `usage`, `performance`, and `cost`. `record_id` must be supplied by the caller and is the duplicate-detection key; validation does not generate IDs or mutate the input mapping.

`cost.total_cost` is optional because a source may not know it. Its absence means unknown, not zero. Likewise, an absent explicit outcome or score/threshold pair means the outcome is unknown, not failed. Aggregates expose completeness status and known subtotals rather than presenting incomplete data as a complete amount.
