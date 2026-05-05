# Security Policy

## Supported versions

Only the latest release receives security patches.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email **security@burnrate.ai** with:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

You'll receive an acknowledgement within 48 hours and a fix timeline within 7 days for confirmed issues.

## Scope

In-scope:
- Authentication bypass
- Anthropic key exposure (plaintext logging, response leakage, DB plaintext storage)
- BurnRate token forgery or bypass
- SQL injection
- SSRF via the proxy
- Cap bypass (reaching the upstream Anthropic API after a `402` kill)

Out of scope:
- Rate limiting on public endpoints (known, intentional)
- Self-XSS
- Issues requiring physical access

## Key material handling

- **Anthropic API keys** are encrypted at rest with [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption (AES-128-CBC + HMAC-SHA256). The key is stored in `FERNET_KEY` env var — never in the database.
- **BurnRate tokens** (`br_*`) are stored as bcrypt hashes. The plaintext token is shown exactly once on creation and never stored.
- **Magic-link tokens** are `itsdangerous.URLSafeTimedSerializer` signed payloads. They expire after 15 minutes.
- We never log the raw `x-api-key` header value, request bodies, or response bodies.
