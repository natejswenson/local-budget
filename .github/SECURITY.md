# Security Policy

local-budget handles personal financial data. It is local-first by design: your
transaction database never leaves your machine, the MCP server runs no
inference, and agent access is gated by a column-level SQLite authorizer.

## Reporting a vulnerability

Please report vulnerabilities **privately** via
[GitHub Security Advisories](https://github.com/natejswenson/local-budget/security/advisories/new)
— do not open a public issue.

Include what you can: affected surface (CLI, MCP tools, web dashboard, Docker),
reproduction steps, and impact. You should get an initial response within a
week.

In scope, especially:

- Bypasses of the SQLite authorizer (reading `raw_ofx`, `payee`, `memo`,
  `acct_hash`, or writing imported transaction rows through any MCP tool,
  including `run_sql`)
- Path traversal in file-writing tools (`save_brief`, `render_report`, upload
  staging)
- Auth/CSRF bypasses in the web dashboard, or getting it to serve non-loopback
  without a valid token
- Account-number redaction failures in tool output

## Supported versions

Only the latest commit on `main` is supported.
