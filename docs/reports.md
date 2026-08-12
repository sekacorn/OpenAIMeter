# Reports

Reports include cost, outcomes, cost per success, providers, anomalies, reconciliation, Prometheus text export, static HTML export, CSV export, JSON export, and FOCUS-inspired Python helpers.

Cost-bearing reports distinguish `known_total_cost` from `total_cost`. The latter is only populated when all included records have known totals; otherwise the report includes an incomplete status and unknown-record count. Provider reports group values by provider and currency and do not perform implicit currency conversion. CSV and FOCUS-style exports leave unknown amounts blank or null. Static HTML escapes user-controlled fields, and CSV export escapes spreadsheet formula prefixes.
