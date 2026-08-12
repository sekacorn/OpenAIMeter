# Pricing

Pricing tables are local YAML files with versioned entries. Rates are expressed as currency amount per 1,000,000 tokens. Resolution requires matching provider, model key, region, currency, and an effective time window. Each entry requires a source reference; entries with overlapping windows for the same provider/model/region/currency are rejected. Missing, out-of-window, ambiguous, or incomplete prices are not treated as zero.

Pricing source management records source references, source type, verification date, effective windows, and optional expiration timestamps. `pricing warnings` reports missing verification dates, stale sources, expired entries, and entries expiring soon. Price arithmetic is only arithmetic under the supplied local table: provider list pricing is not proof of a negotiated rate or billed invoice amount. A billable token category that is priced but absent from the usage record produces an unknown calculation.
