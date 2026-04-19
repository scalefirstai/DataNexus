# Changelog

All notable changes to DataNexus are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Community health files: `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CHANGELOG.md`.
- GitHub Actions CI (`.github/workflows/ci.yml`) — lint + test matrix across
  Python 3.11 / 3.12 / 3.13.
- Dependabot configuration for `pip` and `github-actions`.
- Issue templates (bug report, feature request) and pull request template.
- `Makefile` with `install`, `test`, `run`, `smoke`, `lint`, `format`,
  `clean` targets.
- `docs/architecture.md` — component-level breakdown with a mapping back
  to specification sections.
- `requirements-dev.txt` for lint / test / type-check tooling.
- `pyproject.toml` polish — project metadata, classifiers, URLs,
  optional-dependency groups, tool configuration for Black, Ruff, and
  pytest.

## [1.0.0] — 2026-04-19

Initial public release.

### Added
- REST API (`/api/v1/extracts`) — create, poll, list, cancel, fetch files.
- Event bus with `extract.ready`, `extract.failed`, `extract.expiring`,
  `.dlq` topics, per-consumer-group offsets, at-least-once delivery.
- Object store with atomic staging → publish, `manifest.json` with
  per-file SHA-256 checksums, HMAC-signed presigned URLs.
- Async worker pool with priority queue, exponential backoff with
  jitter, per-fund retry caps, partial-success handling.
- Auth: bearer-token + mTLS fingerprint, fund-level entitlements,
  HMAC-signed webhooks, immutable audit log.
- SLA scaffolding: sliding-window rate limits with `X-RateLimit-*`
  headers, concurrent-extract caps, structured JSON logging, metrics,
  alert rules.
- Idempotency — client `idempotency_key` (replay → 200, drift → 409)
  and consumer-side dedup via `event_id`.
- Retention — `extract.expiring` 24h warning, `EXPIRED` sweep, bus
  pruning.
- Sandbox mirror at `/sandbox/v1/extracts` with full contract parity.
- Runbook hooks — injectable source-outage and partial-failure
  simulation for tests.
- Reference consumer (`consumer_example/`) demonstrating bus
  subscription, presigned-URL download, and checksum verification.
- Test suite covering happy path, idempotency, entitlement denial,
  rate limits, cancel, partial failure, source outage, tampered
  presigned URLs, webhook HMAC verification, and retention sweep.

[Unreleased]: https://github.com/scalefirstai/DataNexus/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/scalefirstai/DataNexus/releases/tag/v1.0.0
