# Security

Security controls include bounded UTF-8 local-file reads, safe YAML loading with duplicate-key rejection, duplicate-key JSON parsing, duplicate record-ID rejection, parameterized SQL, CSV formula escaping, HTML escaping, metadata and structural depth limits, Decimal accounting, and no network requirement for core operations.

These controls provide predictable local input handling; they do not make user-supplied records free of sensitive data. Do not store credentials, private keys, or regulated content in usage records unless the local storage and handling model is appropriate for that data.
