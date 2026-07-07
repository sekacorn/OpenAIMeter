# Pricing

Pricing tables are local YAML files with versioned entries. Rates are expressed as currency amount per 1,000,000 tokens. Resolution considers provider, model key, region, currency, and effective dates. Missing or ambiguous prices are not treated as zero.

Pricing source management records source references, source type, verification date, effective windows, and optional expiration timestamps. `pricing warnings` reports missing verification dates, stale sources, expired entries, and entries expiring soon.
