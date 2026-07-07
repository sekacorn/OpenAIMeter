# Dependency License Report

Runtime dependency:

- PyYAML 6.0.3, MIT license.

Development and verification tools used locally:

- Ruff
- mypy
- pytest
- coverage / pytest-cov
- Bandit
- pip-audit
- build
- Twine

Fresh wheel-environment audit installed current audit tooling and reported no known vulnerabilities for the clean environment dependency set. The package itself was skipped by `pip-audit` because `openaimeter` is not published on PyPI yet.
