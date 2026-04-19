# DataNexus

> **Bulk extract & event-driven distribution for fund-services data — a reference integration pattern.**

[![CI](https://github.com/scalefirstai/DataNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/scalefirstai/DataNexus/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Made by ScaleFirst AI](https://img.shields.io/badge/made%20by-ScaleFirst%20AI-ff69b4.svg)](https://github.com/scalefirstai)

DataNexus is a runnable, contract-first reference implementation of a
**producer–consumer data-distribution pattern** for large, periodic data
extracts. It replaces peak-hour direct reads against operational stores with
a decoupled, auditable pipeline: an API accepts a request, a worker
materialises immutable files into an object store, and downstream applications
are notified via an event bus.

It's built to be *read*, not just run: every design choice maps back to a
numbered section of the [specification](requirements/bulk-extract-distribution-spec.md).

---

## Why it exists

When year-end / quarter-end hits a fund-services estate, every downstream
calculator tries to read the same operational store at the same time. That
couples release schedules, blows through DB connection pools, and leaves
regulators asking why a calc used a mid-run snapshot. DataNexus shows a way
out:

| Symptom | DataNexus response |
|---|---|
| Peak-window contention on the ODS | Extract once, fan out via events + files |
| Schedule coupling between producer and consumer | At-least-once pub/sub with per-group offsets |
| Inconsistent point-in-time reads | `as_of` + immutable, checksum-verified files |
| Entitlement drift at the consumer | Row-level ACLs enforced once at the API tier |
| "Who read what?" in an audit | Every API call, presigned URL, and event publish is logged |

---

## Features

- **REST API** (`/api/v1/extracts`) — create, poll, list, cancel, fetch files
- **Event bus** (in-memory, Kafka-compatible contract) — `extract.ready`,
  `extract.failed`, `extract.expiring`, `.dlq` topics, per-consumer-group offsets
- **Object store** — `/extracts/{domain}/{yyyy}/{mm}/{extract_id}/` layout,
  atomic `staging → rename + _SUCCESS` publish, `manifest.json` with checksums,
  HMAC-signed presigned URLs
- **Async worker pool** — priority queue, exponential backoff with jitter,
  per-fund retry caps, partial-success handling
- **Security** — bearer-token + mTLS fingerprint auth, fund-level
  entitlements, HMAC-signed webhooks, immutable audit log
- **SLA scaffolding** — sliding-window rate limits, `X-RateLimit-*` headers,
  concurrent-extract caps, structured JSON logs, metrics, alert rules
- **Idempotency everywhere** — client `idempotency_key` (replay → 200, drift → 409),
  consumer-side dedup via `event_id`
- **Retention** — `extract.expiring` 24h warning, `EXPIRED` sweep, bus pruning
- **Sandbox mirror** — full contract parity at `/sandbox/v1/extracts`
- **Runbook hooks** — injectable source-outage & partial-failure simulation for tests

---

## Quickstart

```bash
git clone https://github.com/scalefirstai/DataNexus.git
cd DataNexus

python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# run
uvicorn extract_service.main:app --reload

# end-to-end smoke (in another shell)
python scripts/smoke.py

# tests
pytest
```

The service seeds two demo tenants on first boot:

| app_id           | bearer token        | entitled funds                          |
|------------------|---------------------|-----------------------------------------|
| `fes-plus-plus`  | `demo-token-fes`    | fund_A…Z, fund_000…099                  |
| `calc-engine`    | `demo-token-calc`   | fund_A, fund_B                          |

### Submit an extract

```bash
curl -s -X POST http://localhost:8000/api/v1/extracts \
  -H "Authorization: Bearer demo-token-fes" \
  -H "Content-Type: application/json" \
  -d '{
        "domain": "nav-ledger",
        "period": {"start":"2025-01-01","end":"2025-12-31"},
        "fund_scope": ["fund_A","fund_B","fund_C"],
        "output": {"format":"ndjson","compression":"gzip"},
        "notification": {
          "mode":"event",
          "topic":"fund-services.nav-ledger.extract.ready"
        },
        "idempotency_key":"demo-run-001",
        "requester": {"app_id":"fes-plus-plus"}
      }'
```

Then poll `GET /api/v1/extracts/{extract_id}`, fetch
`GET /api/v1/extracts/{extract_id}/files`, and download each
presigned URL. A reference [example consumer](consumer_example/consumer.py)
subscribes to the bus and does this for you.

---

## Architecture

```
┌──────────────┐   1. POST    ┌──────────────┐   2. enqueue   ┌──────────────┐
│  Requesting  │─────────────▶│  Extract API │───────────────▶│   Extract    │
│  Application │◀───202──────│  (FastAPI)   │                │   Worker     │
└──────┬───────┘              └──────┬───────┘                └──────┬───────┘
       │                             │ entitlements, audit,          │
       │ 6. subscribe                │ rate limits, idempotency      │ 4. write
       │                             │                               ▼
       │        ┌──────────────┐     │             ┌──────────────────────────┐
       │◀─7─────│  Event Bus   │◀─5──┴─────────────│  Object Store            │
       │        │ (topics/DLQ) │                   │  atomic staging→publish  │
       │        └──────────────┘                   │  presigned URLs          │
       │                                           └─────────────▲────────────┘
       └─────── 8. download via presigned URL ───────────────────┘
```

More detail in [`docs/architecture.md`](docs/architecture.md) and the
[full specification](requirements/bulk-extract-distribution-spec.md).

---

## Spec coverage

Every section of the spec has a component in the tree:

| Spec § | Topic | Implementation |
|---|---|---|
| 3 | REST API | [`extract_service/main.py`](extract_service/main.py), [`extract_service/models.py`](extract_service/models.py) |
| 4 | Event bus (topics, groups, DLQ, retention) | [`extract_service/event_bus.py`](extract_service/event_bus.py) |
| 4.7 | Webhooks (HMAC, retry schedule) | [`extract_service/webhooks.py`](extract_service/webhooks.py) |
| 5 | Object store (layout, manifest, atomic publish, presigned URLs) | [`extract_service/object_store.py`](extract_service/object_store.py) |
| 6 | Auth + entitlements | [`extract_service/auth.py`](extract_service/auth.py) |
| 7 | Errors, retries, partial success, idempotency | [`extract_service/worker.py`](extract_service/worker.py), [`extract_service/errors.py`](extract_service/errors.py) |
| 8 | Rate limits, concurrent-extract cap | [`extract_service/auth.py`](extract_service/auth.py) |
| 9 | Structured logs, metrics, alerts | [`extract_service/logging_config.py`](extract_service/logging_config.py), [`extract_service/metrics.py`](extract_service/metrics.py), [`extract_service/alerts.py`](extract_service/alerts.py) |
| 10 | Versioning (`/api/v1`, `event_version`, `schema_version`) | models + routers |
| 11 | Sandbox (`/sandbox/v1`) + consumer onboarding | [`consumer_example/`](consumer_example/) |
| 12 | Runbook hooks (outage, partial, cancel) | worker simulation hooks |
| App. A | Domain registry | [`extract_service/domains.py`](extract_service/domains.py) |

---

## Testing

```bash
pytest                       # 14 tests, ~5s
pytest --cov=extract_service # with coverage (install pytest-cov first)
```

The suite covers happy-path lifecycle, event emission, idempotency
(replay → 200, drift → 409), entitlement denial, rate-limit headers,
cancel idempotency, partial failure → `PARTIAL`, source outage →
`FAILED`, tampered presigned URL rejection, entitlement-scoped file
listing, webhook HMAC verification, and retention sweep (`extract.expiring`
→ `EXPIRED`).

### Exercised with a real large-file workload

25 funds × ~500K rows per fund (NDJSON + gzip):

| Metric | Value |
|---|---|
| Files produced | 25 |
| Rows | 19,747,200 |
| Compressed size | 220 MB |
| Worker duration | 257 s |
| Checksums verified | 25 / 25 |
| Tampered presigned URL | 403 ✓ |
| Idempotent replay | 200 ✓ |
| Conflicting replay | 409 ✓ |

---

## Production substitutions

DataNexus runs in a single process so the contract can be exercised without
external infrastructure. Every module boundary was drawn to make the
substitution obvious:

| Component | Reference | Production |
|---|---|---|
| Object store | local FS + HMAC signer | S3 / GCS with native presigned URLs |
| Event bus | in-memory | Kafka / Kinesis / Pub-Sub |
| DB | SQLite WAL | Postgres with row-level locks |
| Auth | token lookup + header mTLS | OAuth 2.0 client-credentials + mTLS at ingress |
| Worker | asyncio task pool | K8s / ECS worker pool with priority queues |
| Retention | in-process sweeper | cron / lifecycle rules on bucket + scheduled DB job |

The spec guarantees (atomic publish, at-least-once delivery, entitlement
boundary, idempotency) are preserved across all of these substitutions.

---

## Documentation

- [**Specification**](requirements/bulk-extract-distribution-spec.md) — the contract
- [`docs/architecture.md`](docs/architecture.md) — component breakdown
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, commit style, PR flow
- [`SECURITY.md`](SECURITY.md) — how to report vulnerabilities
- [`CHANGELOG.md`](CHANGELOG.md) — release notes

---

## Contributing

PRs welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md). All
contributors are expected to follow the
[Contributor Covenant](CODE_OF_CONDUCT.md).

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

Copyright © 2026 [ScaleFirst AI](https://github.com/scalefirstai).
