# DataNexus

> **A spec-first reference implementation of event-driven bulk data distribution for fund services — runnable, auditable, and contract-verified.**

[![CI](https://github.com/scalefirstai/DataNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/scalefirstai/DataNexus/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Made by ScaleFirst AI](https://img.shields.io/badge/made%20by-ScaleFirst%20AI-ff69b4.svg)](https://github.com/scalefirstai)

## The problem

In fund services, year-end and quarter-end close are the single
largest source of peak-window failures. Every downstream calculator,
report, and prospectus engine tries to read the same operational data
store at the same time — couple release schedules break, connection
pools saturate, and regulators ask uncomfortable questions about why
a calc used a mid-run snapshot.

**DataNexus is a reference design for decoupling those reads.** An
API accepts a request, a worker materialises immutable files into an
object store, and downstream applications are notified via an event
bus. Consumers fetch files through presigned, entitlement-scoped URLs
— never by re-reading the operational store.

It is built to be **read, not just run**. Every design choice maps
back to a numbered section of the
[specification](requirements/bulk-extract-distribution-spec.md) so
that the spec and the running code verify each other.

## What this actually is

Framing matters. DataNexus is:

- A **contract executable** (a runnable conformance suite for the
  spec), not a production platform. The in-memory event bus, local
  filesystem object store, and SQLite dev DB are stand-ins — see
  *Production substitutions* below for the swap-in path.
- A **composition of Enterprise Integration Patterns** (Hohpe &
  Woolf, 2003) — specifically Claim Check, File Transfer,
  Publish-Subscribe Channel with Durable Subscriber, Dead Letter
  Channel, Idempotent Receiver, and Correlation Identifier —
  specialised for fund-services batch workloads.
- Deliberately narrow: **periodic batch extracts with fund-level
  entitlement in a regulated environment**. Not a data mesh, not a
  lakehouse, not CDC, not zero-copy sharing. See *When not to use
  this* below.

The niche is the pitch: **for fund-services estates where data lives
across Eagle, Geneva, InvestOne, Investran, and a warehouse — not all
in one Snowflake account — DataNexus is the contract that decouples
producers from consumers without requiring a migration of the whole
estate.**

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
| Event bus | in-memory (**contract emulator**, see note) | Kafka / Kinesis / Pub-Sub |
| DB | SQLite WAL | Postgres with row-level locks (`SELECT … FOR UPDATE SKIP LOCKED`) |
| Auth | token lookup + header mTLS | OAuth 2.0 client-credentials + mTLS at ingress |
| Worker | asyncio task pool | Postgres `SKIP LOCKED` **or** Kafka consumer groups **or** K8s Jobs (§8.4.1 of the spec) |
| Retention | in-process sweeper | cron / lifecycle rules on bucket + scheduled DB job (with the §5.5.1 URL-before-delete invariant preserved) |

The spec guarantees (atomic publish, at-least-once delivery, entitlement
boundary, idempotency, URL-expiry-before-file-delete, correlation-id
propagation) are preserved across all of these substitutions.

> **About the in-memory bus.** It is a **contract emulator** — it
> implements the per-consumer-group offset / DLQ / retention surface
> that the spec assumes, and nothing more. It is **not** a Kafka
> drop-in: the reference bus does not implement log compaction,
> consumer-group rebalancing, EOS transactions, or partitioner choice.
> Migrating to real Kafka surfaces decisions the reference doesn't
> force — partitioning (`extract_id` vs `domain` vs `fund_id`),
> `acks=all` + `min.insync.replicas=2` on the publish path, cooperative
> rebalancing during long-running extracts, and so on.

---

## When **not** to use DataNexus

A few explicit non-goals, so the scope stays honest:

- **CDC / streaming replication** — if you need change-data-capture
  or sub-second streaming materialised views, use Debezium / Kafka
  Connect / Fivetran. DataNexus is a batch contract.
- **Zero-copy cross-tenant sharing** — if both sides live in
  Snowflake, use Snowflake Secure Data Sharing. DataNexus exists
  because the sides don't live in the same warehouse.
- **Analytical table sharing** — if the consumer is a BI tool or a
  Spark/pandas job and wants a **table** (not a per-run file
  bundle), Delta Sharing or Iceberg REST is a better fit.
- **Synchronous request/response data access** — DataNexus is
  explicitly asynchronous. If a consumer needs sub-second reads,
  build a query API, not an extract pipeline.
- **Intra-team DAG orchestration** — if both producer and consumer
  live inside the same Airflow cluster, Airflow Assets / Datasets is
  the right tool; DataNexus is cross-team, cross-system.

### When to use X instead

| You have… | Use this | Because… |
|---|---|---|
| Single-cloud AWS shop, no cross-org sharing | **S3 + S3 Event Notifications → SNS/SQS/EventBridge** | You'd build DataNexus on top of this anyway. If you don't need the API-tier entitlement layer or the logical-run semantics, skip the layer. |
| Analytical consumers (Spark, Trino, BI) wanting a table | **Delta Sharing** or **Apache Iceberg + REST catalog** | Pull model with time-travel is a better fit for analytics than a per-run push. DataNexus can front a Delta Sharing endpoint once files land if you want both. |
| All data lives in Snowflake today | **Snowflake Secure Data Sharing** | Zero-copy, no extract, no retention sweep. Genuinely different architecture. |
| Producer and consumer are both Airflow DAGs in the same cluster | **Airflow Datasets / Assets** | Cross-DAG dependency via outlets is simpler when the scheduler is shared. |
| You specifically want: cross-system (not single-cloud), API-tier entitlement per-fund, first-class idempotency contract, and a push-notification contract that survives Kafka substitution | **DataNexus** | That exact combination is underserved; every alternative above weakens one of those axes. |

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
