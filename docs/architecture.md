# Architecture

DataNexus is a single-process reference implementation of the bulk
extract + event-distribution contract defined in
[`requirements/bulk-extract-distribution-spec.md`](../requirements/bulk-extract-distribution-spec.md).
Every module boundary was drawn so that the reference backend (in-memory
bus, local filesystem object store, SQLite dev DB) can be swapped for a
production backend without changing the contract.

This document is a map, not a tutorial — read it alongside the
spec and the source.

---

## Component map

```
                     ┌────────────────────────────────────────────────────────┐
                     │                    extract_service                      │
                     │                                                         │
  client ──HTTP──▶   │  main.py          ── §3 REST API, §10 versioning       │
                     │    │                                                    │
                     │    ├── auth.py    ── §6 auth, entitlements, rate limits │
                     │    ├── models.py  ── §3/§4 request + event schemas      │
                     │    ├── errors.py  ── §7 error taxonomy                  │
                     │    ├── domains.py ── App.A domain registry              │
                     │    ├── storage.py ── §7 idempotency + audit persistence │
                     │    ├── config.py  ── env + defaults                     │
                     │    │                                                    │
                     │    ▼ enqueue                                            │
                     │  worker.py        ── §7 retries, backoff, partial OK    │
                     │    │                                                    │
                     │    ├── object_store.py ── §5 layout, manifest, URLs     │
                     │    ├── event_bus.py    ── §4 topics, groups, DLQ       │
                     │    ├── webhooks.py     ── §4.7 HMAC delivery + sweeper  │
                     │    ├── metrics.py      ── §9 counters, histograms       │
                     │    ├── alerts.py       ── §9 alert rules                │
                     │    └── logging_config  ── §9 structured JSON logs       │
                     └────────────────────────────────────────────────────────┘
                                     │
                                     ▼ subscribe / download
                               consumer_example/     ── §11 reference consumer
```

### One-line module summaries

| Module | Spec § | Responsibility |
|---|---|---|
| `main.py` | 3, 10 | FastAPI app. Request parsing, auth wiring, idempotency gate, response envelopes. |
| `auth.py` | 6, 8 | Bearer + mTLS fingerprint auth, fund-level entitlements, sliding-window rate limiter, concurrent-extract cap. |
| `models.py` | 3, 4 | Pydantic models for API requests/responses, event payloads, status enums. |
| `errors.py` | 7 | Domain-specific exceptions + the `ErrorCode` enum used in HTTP/event payloads. |
| `domains.py` | App. A | Domain registry — schemas, default partition keys, source mapping. |
| `storage.py` | 7 | Idempotency fingerprinting, audit log, extract state persistence. SQLite in dev. |
| `worker.py` | 7 | Async worker pool. Priority queue, exponential backoff with jitter, per-fund retry cap, partial-success handling, runbook simulation hooks. |
| `object_store.py` | 5 | `/extracts/{domain}/{yyyy}/{mm}/{extract_id}/` layout, atomic `staging → rename + _SUCCESS`, `manifest.json` with SHA-256, HMAC-signed presigned URLs. |
| `event_bus.py` | 4 | In-memory topic log with per-consumer-group offsets, DLQ, retention pruning. Kafka-compatible API shape. |
| `webhooks.py` | 4.7 | HMAC-signed webhook delivery with retry schedule + retention sweeper that emits `extract.expiring`. |
| `metrics.py` | 9 | Counters, histograms, and gauges exposed for scraping. |
| `alerts.py` | 9 | Alert rule definitions (breach windows, thresholds). |
| `logging_config.py` | 9 | Structured JSON logging, request-scoped context. |
| `consumer_example/consumer.py` | 11 | Reference consumer: subscribes, downloads via presigned URL, verifies SHA-256, commits offset. |

---

## Request lifecycles

### Successful extract

```
 POST /api/v1/extracts
   ├─ authenticate            (auth.py)
   ├─ check_entitlements      (auth.py)   — denies un-entitled funds per §6.3
   ├─ rate-limit check        (auth.py)   — sliding window per app_id
   ├─ idempotency_key lookup  (storage.py) — replay returns original 202, drift raises 409
   ├─ persist extract row     (storage.py)
   └─ enqueue on worker pool  (worker.py)  → 202 Accepted

 Worker picks up:
   ├─ simulate source I/O (or real source in prod)
   ├─ write files to staging   (object_store.py)
   ├─ atomic rename + write _SUCCESS + manifest.json
   ├─ mark extract READY       (storage.py)
   ├─ publish extract.ready    (event_bus.py)
   └─ fire webhook if notification.mode == "webhook"  (webhooks.py)

 Consumer:
   ├─ subscribe to topic / receive webhook
   ├─ GET /api/v1/extracts/{id}/files   → presigned URLs (HMAC)
   ├─ download, verify SHA-256 against manifest
   └─ commit offset
```

### Failure + retry

`worker.py` classifies failures as *transient* (retry with jittered
exponential backoff up to `max_attempts`) or *terminal* (publish
`extract.failed`, write to DLQ). Per-fund retry caps prevent one bad
fund from starving the rest of the extract — survivors publish as
`PARTIAL` per §7.5.

### Retention

`webhooks.get_retention_sweeper` runs periodically:

1. For extracts within 24h of expiry → emit `extract.expiring`.
2. For extracts past expiry → mark `EXPIRED`, delete files, prune bus.

Consumers that missed the window get a clear terminal state instead of
a silent 404.

---

## Contract guarantees (preserved across backend substitutions)

| Guarantee | Where enforced | Why it survives substitution |
|---|---|---|
| Atomic publish (no partial reads) | `object_store.py` — staging → rename + `_SUCCESS` | Same pattern maps to S3 multi-part + final `_SUCCESS` marker. |
| At-least-once delivery | `event_bus.py` — offsets per consumer group | Native on Kafka/Kinesis/Pub-Sub. |
| Entitlement boundary | `auth.py` — enforced at API tier only | Consumers never need to re-check; contract is single-source. |
| Idempotency | `storage.py` — fingerprint of normalised payload | Swap SQLite for Postgres; fingerprint logic unchanged. |
| Tamper-evident URLs | `object_store.py` — HMAC-signed query params | Replace with S3 SigV4 presigned URLs without changing callers. |
| Webhook authenticity | `webhooks.py` — HMAC over body + timestamp | Standard pattern; no consumer-side change needed. |

---

## Production substitutions

See the README's [Production substitutions](../README.md#production-substitutions)
table. The short version: every module in `extract_service/` is
importable behind a factory (`get_event_bus`, `get_object_store`,
`get_storage`, `get_rate_limiter`, `get_webhook_delivery`,
`get_retention_sweeper`) so swapping implementations is a wiring
change, not a rewrite.

---

## Testing strategy

- **Contract tests** exercise the FastAPI app in-process using `httpx.ASGITransport`.
- **Worker tests** inject runbook simulation hooks to force source
  outages and partial failures deterministically.
- **Retention tests** use a monkey-patched clock rather than `sleep`.
- **Webhook tests** verify HMAC signatures end-to-end.

No test mocks the object store or event bus — the reference backends
are cheap enough to exercise real-ly, which means a test that passes
here is a test that exercises the contract, not a seam.
