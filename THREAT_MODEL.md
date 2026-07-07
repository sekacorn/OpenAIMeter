# Threat Model

OpenAIMeter handles local economic telemetry. Primary risks are secret leakage in metadata, unsafe spreadsheet exports, malicious YAML, SQL injection attempts, path misuse in automation, and overstated cost accuracy.

Controls in alpha include safe YAML loading, parameterized SQL, CSV formula escaping, explicit economic status fields, local-only storage defaults, and documentation that discourages storing secrets.
