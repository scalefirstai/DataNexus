# Changelog

All notable changes to DataNexus are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-04-19

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
- `tests/test_spec_conformance.py` — CI-enforced requirement that every
  numbered spec section is referenced by at least one test (or listed in
  a deliberate exemption).
- `tests/test_observability.py` — correlation-id minting + echoing +
  metrics emission on the happy path.

### Changed — spec v1.0 → v1.1 amendments (following design review)

Spec source: `requirements/bulk-extract-distribution-spec.md`. Changes
are additive where possible so v1.0 consumers keep working; bumped
`event_version` on `extract.expiring` from `1.0` to `1.1` to signal
the new `scope` / `requester` / `idempotency_key` fields.

- `PARTIAL` promoted to a first-class terminal status (§3.2.2, §7.3).
  The status table now lists it; the `TERMINAL_STATUSES` set in code
  already treated it that way.
- `extract.expiring` event gains `scope`, `requester`, and
  `idempotency_key` blocks for symmetry with `extract.ready` /
  `extract.failed` (§4.5). Consumer libraries can now treat all three
  event types uniformly.
- `GET /extracts/{id}/files` returns **`410 Gone`** when the extract
  is `EXPIRED` (§3.2.3). Replays of old events now get a terminal
  signal instead of a misleading `404`.
- Retention vs in-flight download invariant specified (§5.5.1):
  **presigned URL expiry is strictly less than file deletion time**.
  The sweep is two-phase — quiesce (stop issuing new URLs) then delete
  after `url_ttl + safety_margin`.
- HMAC secret rotation semantics (§4.7.1). 90-day cadence, 14-day
  overlap window during which both old and new signatures verify.
  Compressed 1-hour overlap for suspected compromise.
- Domain lifecycle (App. A): `PROPOSED` → `ACTIVE` → `DEPRECATED`
  → `RETIRED`. API behaviour per state is now defined; `DEPRECATED`
  completes in-flight extracts, `RETIRED` returns `410 Gone`.
- `as_of` semantics are now **domain-local** and must be documented
  per-domain in the registry (App. A.2). Bi-temporal sources give
  exact point-in-time; snapshot sources give last-commit-at-or-before;
  opaque sources reject with `AS_OF_NOT_SUPPORTED`.
- Versioning interaction matrix added (§10.4). "The most conservative
  window wins" when API, event-schema, and data-schema changes
  compose.
- Worker scale-out mechanism documented (§8.4.1) — Postgres
  `SELECT … FOR UPDATE SKIP LOCKED` as the default, with Kafka
  consumer groups and K8s Jobs as alternatives.
- Correlation-id enforcement principle stated (§9.4.1): the API
  mints one if the client does not provide it, echoes it in
  `X-Correlation-Id`, and a missing value on any downstream hop is
  a contract violation.
- Per-tenant CMK / envelope encryption on the manifest added (§6.5).
  Compromise of a bearer token is no longer the whole entitlement
  boundary.
- Polling-only notification (`notification.mode = none`) demoted to
  a recovery fallback with a stricter rate limit (10 polls/min vs
  60/min) — §3.2.1, §3.2.2, §8.3.
- Requester `app_id` information-disclosure note added (§4.3) — some
  deployments may redact on fan-out while preserving the value in the
  audit log.
- `_SUCCESS` marker is now explicitly stated as **not** part of
  `manifest.files[]` — it is a publish-protocol artefact (§5.3, §5.4).
- `retry_after_seconds` body field (§3.2.1) is deprecated in favour of
  the `Retry-After` header for RFC 9110 consistency.

### Changed — README framing (following design review)

- Tagline rewritten from "Bulk extract & event-driven distribution…"
  to "A spec-first reference implementation of event-driven bulk data
  distribution for fund services — runnable, auditable, and
  contract-verified."
- Lead with the year-end problem, not the pattern.
- New *"What this actually is"* section positioning DataNexus as a
  **contract executable** (not a production platform) and as a
  **composition of Enterprise Integration Patterns** (Claim Check,
  File Transfer, Pub-Sub with Durable Subscriber, Dead Letter
  Channel, Idempotent Receiver, Correlation Identifier).
- New *"When not to use DataNexus"* section listing explicit
  non-goals: CDC, zero-copy sharing, analytical table sharing,
  synchronous access, intra-cluster DAG orchestration.
- New *"When to use X instead"* table covering S3 + EventBridge,
  Delta Sharing / Iceberg, Snowflake Secure Data Sharing, and
  Airflow Datasets.
- Production-substitutions table now names the in-memory bus as a
  **contract emulator** (not a Kafka drop-in) and points at
  §5.5.1 / §8.4.1 for the production-migration details.

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

[Unreleased]: https://github.com/scalefirstai/DataNexus/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/scalefirstai/DataNexus/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/scalefirstai/DataNexus/releases/tag/v1.0.0
