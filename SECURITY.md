# Security

Report security issues privately through the repository security advisory process once the public repository exists.

OpenAIMeter is local-first and should not receive secrets in usage records. The CLI uses safe YAML loading, parameterized SQLite queries, CSV formula escaping, and metadata depth limits.
