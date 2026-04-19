# Security Policy

DataNexus is a reference implementation, but its contract (entitlements,
HMAC-signed URLs, webhook signatures, audit log, idempotency) is the
entire point — we take reports against that contract seriously.

## Supported versions

| Version | Supported |
|---------|-----------|
| `main`  | Yes       |
| `1.x`   | Yes       |
| `< 1.0` | No        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for a suspected vulnerability.**

Instead, email **security@scalefirstai.dev** with:

- A description of the issue and the impact you believe it has.
- Steps to reproduce — ideally a minimal test case against `main`.
- Affected version / commit SHA.
- Any relevant logs, PoC, or suggested remediation.

If you prefer encrypted email, request our PGP key in an initial
unencrypted message and we will reply with the fingerprint.

## What to expect

- **Acknowledgement** within 3 business days.
- **Triage + severity assessment** within 7 business days.
- **Fix or mitigation plan** shared with the reporter before public
  disclosure.
- **Coordinated disclosure** — we will agree a disclosure timeline with
  the reporter (target: 90 days, shorter for actively-exploited issues).
- **Credit** in the release notes, unless the reporter prefers to remain
  anonymous.

## Out of scope

- Issues that depend on compromising the local developer environment
  (e.g. stealing the demo bearer tokens from a running dev instance).
- Denial-of-service that requires resource limits beyond the documented
  defaults (the rate limiter and concurrent-extract cap are the boundary).
- Findings against the in-memory stubs (event bus, object store, SQLite
  dev DB) that would not apply to a production substitution listed in
  the README's *Production substitutions* table.
- Social-engineering of maintainers.

## Hall of fame

Reporters who follow this process will be listed in
[`CHANGELOG.md`](CHANGELOG.md) alongside the fixed version, unless they
request otherwise.
