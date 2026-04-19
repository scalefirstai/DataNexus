# Bulk Extract & Event-Driven Distribution — Technical Specification

**Version:** 1.1  
**Status:** DRAFT  
**Date:** 2026-04-19  
**Owner:** Fund Services Architecture  
**Audience:** Engineering, Platform, Security, Operations

**Changelog (1.1):** amendments following the 2026-04-19 design review
— `PARTIAL` promoted to first-class terminal status (§3.2.2, §7.3);
`scope` and `requester` added to `extract.expiring` (§4.5); retention
vs in-flight-download invariant specified (§5.5); 410 Gone added for
expired extracts (§3.2.3); versioning interaction matrix added (§10.4);
HMAC secret rotation specified (§4.7); domain lifecycle + per-domain
`as_of` semantics added (App. A); `retry_after_seconds` body field
deprecated in favour of the `Retry-After` header (§3.2.1); polling-only
notification mode demoted to a recovery fallback with stricter rate
limit (§3.2.1, §8.3); correlation-id enforcement principle stated
(§9.4); per-tenant KMS / envelope encryption added (§6.5); worker
scale-out mechanism documented (§8.4).

---

## 1. Purpose & Scope

This specification defines the app-to-app communication contract for **scheduled bulk data extraction and event-driven distribution** across the fund services estate. It replaces direct synchronous reads against the operational data store during peak windows (year-end, quarter-end) with a decoupled, producer-consumer architecture.

### 1.1 In Scope

- REST API contract for extract lifecycle management (request, status, retrieval, cancellation)
- Event bus contract for asynchronous completion notification and fan-out
- Object storage layout, naming, partitioning, and retention
- Security model (authentication, authorisation, encryption, entitlements)
- SLA framework (latency, availability, throughput, data freshness)
- Error handling, retry semantics, dead-letter processing
- Observability and operational SLIs/SLOs

### 1.2 Out of Scope

- Internal implementation of the extract worker (query optimisation, warehouse tuning)
- Consumer-side processing logic (how a calc service uses the data)
- Data model / schema of the extracted NAV/Ledger records (covered by the domain data contract)
- Infrastructure provisioning and deployment topology

### 1.3 Design Principles

1. **Data through storage, metadata through the bus.** Events carry pointers, never payload.
2. **Consumers never call the producer.** All interaction is mediated by the API, the object store, or the bus.
3. **Immutability after publish.** Corrections produce new runs, not overwrites.
4. **Additive schema evolution.** New fields are added; existing fields are never removed or re-purposed.
5. **Entitlements at the boundary.** Row-level access control is enforced once, at the API tier, not delegated to downstream systems.
6. **Idempotency everywhere.** Every operation is safe to retry without side effects.

---

## 2. System Context

### 2.1 Actors

| Actor | Role | Communication |
|---|---|---|
| **Requesting Application** | Initiates an extract request and consumes the result | REST API (sync) + Event Bus (async) |
| **Extract API** | Accepts requests, manages lifecycle, enforces entitlements | REST API |
| **Extract Worker** | Executes the query, materialises files, publishes events | Internal (not exposed) |
| **Object Store** | Holds immutable, partitioned extract files | Presigned URL access |
| **Event Bus** | Distributes completion/failure notifications | Topic-based pub/sub |
| **Data Catalog** | Registers schema, partitions, lineage | Internal (not exposed) |
| **Monitoring** | Collects SLIs, fires alerts | Internal |

### 2.2 Communication Flow Summary

```
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│  Requesting   │──(1)──▶│  Extract API  │──(2)──▶│   Extract    │
│  Application  │◀──(3)──│              │        │   Worker     │
└──────┬───────┘        └──────────────┘        └──────┬───────┘
       │                                               │
       │ (6) subscribe                         (4) write│
       │                                               ▼
       │         ┌──────────────┐        ┌──────────────┐
       │◀──(7)───│  Event Bus   │◀──(5)──│ Object Store │
       │         └──────────────┘        └──────────────┘
       │                                        ▲
       └────────────────(8) fetch files─────────┘

(1) POST /extracts            → 202 Accepted + request_id
(2) Internal dispatch         → worker picks up job
(3) GET  /extracts/{id}       → status polling (fallback)
(4) Worker writes files       → partitioned, compressed, immutable
(5) Worker publishes event    → extract.ready / extract.failed
(6) Consumer subscribes       → topic + consumer group
(7) Event delivered           → contains file location, not data
(8) Consumer fetches files    → presigned URL, scoped by entitlement
```

---

## 3. REST API Contract

### 3.1 Base Path

```
/api/v1/extracts
```

All endpoints require authentication (see §6). All request and response bodies are `application/json`. All timestamps are ISO 8601 UTC.

### 3.2 Endpoints

#### 3.2.1 Create Extract Request

```
POST /api/v1/extracts
```

**Purpose:** Submit a new extract request. The API validates, persists, and enqueues the job. Returns immediately with a tracking resource.

**Request Body:**

```json
{
  "domain": "nav-ledger",
  "period": {
    "start": "2025-01-01",
    "end": "2025-12-31"
  },
  "as_of": "2026-01-02T23:59:59Z",
  "fund_scope": ["fund_A", "fund_B", "fund_C"],
  "frequency": "yearly",
  "output": {
    "format": "parquet",
    "compression": "gzip",
    "partition_by": "fund_id"
  },
  "notification": {
    "mode": "event",
    "topic": "fund-services.extract.ready",
    "callback_url": null
  },
  "idempotency_key": "fes-yearly-2025-run-001",
  "priority": "normal",
  "requester": {
    "app_id": "fes-plus-plus",
    "correlation_id": "corr-20260103-0417"
  }
}
```

**Field Reference:**

| Field | Type | Required | Description |
|---|---|---|---|
| `domain` | string | yes | Business domain identifier. Controls which source tables are queried. |
| `period.start` | date | yes | Inclusive start of the extract window. |
| `period.end` | date | yes | Inclusive end of the extract window. |
| `as_of` | datetime | no | Point-in-time for data versioning. Default: latest available. |
| `fund_scope` | string[] | no | Explicit list of fund identifiers. Default: all entitled funds. |
| `frequency` | enum | no | Hint for the worker: `daily`, `monthly`, `quarterly`, `yearly`. Default: inferred from period. |
| `output.format` | enum | yes | `parquet`, `ndjson`, `csv`. |
| `output.compression` | enum | no | `gzip`, `zstd`, `none`. Default: `gzip`. |
| `output.partition_by` | string | no | Partition key for output files. Default: `fund_id`. |
| `notification.mode` | enum | no | `event` (bus), `webhook` (HTTP callback), `none` (polling-only recovery fallback — **not recommended for new integrations**, see §3.2.2 and §8.3 for stricter poll rate limits). Default: `event`. |
| `notification.topic` | string | cond. | Required if mode is `event`. The topic the completion event is published to. |
| `notification.callback_url` | string | cond. | Required if mode is `webhook`. HTTPS URL the API will POST completion to. |
| `idempotency_key` | string | yes | Client-provided key. Duplicate submissions with the same key return the existing resource, not a new job. Max 128 chars. |
| `priority` | enum | no | `high`, `normal`, `low`. Affects worker queue ordering. Default: `normal`. |
| `requester.app_id` | string | yes | Registered application identifier (from the service registry). |
| `requester.correlation_id` | string | no | Client-side trace ID for end-to-end correlation. |

**Response — 202 Accepted:**

```json
{
  "extract_id": "ext_20260103_041722_a7f3",
  "status": "ACCEPTED",
  "created_at": "2026-01-03T04:17:22Z",
  "links": {
    "self": "/api/v1/extracts/ext_20260103_041722_a7f3",
    "cancel": "/api/v1/extracts/ext_20260103_041722_a7f3/cancel"
  },
  "estimated_completion": "2026-01-03T04:25:00Z"
}
```

**Response Headers:**

| Header | Value |
|---|---|
| `Location` | `/api/v1/extracts/ext_20260103_041722_a7f3` |
| `Retry-After` | `30` |
| `X-Correlation-ID` | echoed from the request, or API-generated if absent (see §9.4) |

> **Note.** The `retry_after_seconds` body field present in v1.0 was
> deprecated in v1.1 in favour of the `Retry-After` response header
> (RFC 9110). Clients MUST prefer the header. The API may continue
> emitting the body field for one minor version for backwards
> compatibility; new clients MUST NOT rely on it.

**Error Responses:**

| Status | Condition |
|---|---|
| `400 Bad Request` | Validation failure (missing fields, invalid period, unknown domain). |
| `401 Unauthorized` | Missing or invalid authentication. |
| `403 Forbidden` | App not entitled to requested fund_scope. |
| `409 Conflict` | Idempotency key matches an existing request with different parameters. |
| `429 Too Many Requests` | Rate limit exceeded. `Retry-After` header included. |

#### 3.2.2 Get Extract Status

```
GET /api/v1/extracts/{extract_id}
```

**Purpose:** Poll for the current status of an extract. This is the fallback mechanism when event-based notification is unavailable or as a recovery check.

**Response — 200 OK (in progress):**

```json
{
  "extract_id": "ext_20260103_041722_a7f3",
  "status": "RUNNING",
  "progress": {
    "funds_completed": 2,
    "funds_total": 3,
    "percent": 67,
    "current_fund": "fund_B"
  },
  "created_at": "2026-01-03T04:17:22Z",
  "updated_at": "2026-01-03T04:20:14Z",
  "estimated_completion": "2026-01-03T04:23:00Z"
}
```

**Response — 200 OK (completed):**

```json
{
  "extract_id": "ext_20260103_041722_a7f3",
  "status": "COMPLETED",
  "created_at": "2026-01-03T04:17:22Z",
  "completed_at": "2026-01-03T04:22:48Z",
  "duration_seconds": 326,
  "result": {
    "location": "/api/v1/extracts/ext_20260103_041722_a7f3/files",
    "file_count": 3,
    "total_bytes": 184320000,
    "total_rows": 2847103,
    "checksum": "sha256:9f4b2c...",
    "schema_version": "nav-ledger/v3",
    "expires_at": "2026-01-10T04:22:48Z"
  },
  "lineage": {
    "sources": ["ods.nav_positions", "warehouse.gl_entries"],
    "as_of": "2026-01-02T23:59:59Z",
    "run_id": "run_20260103_041722"
  }
}
```

**Status Values:**

| Status | Description | Terminal? |
|---|---|---|
| `ACCEPTED` | Request validated and queued. Worker has not started. | No |
| `RUNNING` | Worker is actively extracting and writing files. | No |
| `COMPLETED` | All files written, validated, and published. Event emitted. | Yes |
| `PARTIAL` | Some funds succeeded and others exhausted retries. Partial files are published and available; a `partial_result` block lists `funds_completed` / `funds_failed`. Consumers decide whether partial data is usable. See §7.3. | Yes |
| `FAILED` | Unrecoverable error with no usable output. See `error` object. | Yes |
| `CANCELLED` | Cancelled by client or system. | Yes |
| `EXPIRED` | Result files have been removed per retention policy. See §3.2.3 for `410 Gone` behaviour on the files endpoint. | Yes |

**Polling rate limit.** Status polling is intended as a recovery
mechanism, not the primary notification path. Clients that requested
`notification.mode = none` are subject to a stricter rate limit of
**10 polls/minute per app_id** (vs 60/minute for apps using the bus or
webhook) — see §8.3. Consumers that require low-latency completion
signals MUST use `event` or `webhook` notification.

#### 3.2.3 List Extract Files

```
GET /api/v1/extracts/{extract_id}/files
```

**Purpose:** Returns presigned, time-limited URLs for each file in the completed extract. Only available when status is `COMPLETED`.

**Response — 200 OK:**

```json
{
  "extract_id": "ext_20260103_041722_a7f3",
  "files": [
    {
      "file_id": "file_001",
      "partition_key": "fund_A",
      "format": "parquet",
      "compression": "gzip",
      "size_bytes": 62140000,
      "row_count": 948201,
      "checksum": "sha256:a1b2c3...",
      "download_url": "https://storage.internal/extracts/ext_.../fund_A.parquet.gz?token=...",
      "url_expires_at": "2026-01-03T05:22:48Z"
    },
    {
      "file_id": "file_002",
      "partition_key": "fund_B",
      "format": "parquet",
      "compression": "gzip",
      "size_bytes": 58920000,
      "row_count": 903412,
      "checksum": "sha256:d4e5f6...",
      "download_url": "https://storage.internal/extracts/ext_.../fund_B.parquet.gz?token=...",
      "url_expires_at": "2026-01-03T05:22:48Z"
    },
    {
      "file_id": "file_003",
      "partition_key": "fund_C",
      "format": "parquet",
      "compression": "gzip",
      "size_bytes": 63260000,
      "row_count": 995490,
      "checksum": "sha256:g7h8i9...",
      "download_url": "https://storage.internal/extracts/ext_.../fund_C.parquet.gz?token=...",
      "url_expires_at": "2026-01-03T05:22:48Z"
    }
  ],
  "manifest": {
    "download_url": "https://storage.internal/extracts/ext_.../manifest.json?token=...",
    "url_expires_at": "2026-01-03T05:22:48Z"
  }
}
```

**Notes:**
- Presigned URLs expire after **1 hour** by default. Consumers that need longer access should re-call this endpoint for fresh URLs.
- URLs are scoped to the requesting application's entitlements. A consumer entitled to fund_A and fund_B will not receive a URL for fund_C.
- The `manifest.json` file contains the full run metadata (checksums, row counts, schema version, lineage) and is always included.
- The `_SUCCESS` marker is intentionally **not** included in the manifest's `files[]` list (§5.3) or in the API response — it is a publish-protocol artefact only (§5.4), and checksum-verifying consumers would otherwise flag a benign extra file.

**Error Responses:**

| Status | Condition |
|---|---|
| `200 OK` | Extract is `COMPLETED` or `PARTIAL`. Files that the caller is entitled to are returned. |
| `401` / `403` | Standard auth and entitlement errors. |
| `404 Not Found` | The `extract_id` does not exist, or is not visible to the caller. |
| `409 Conflict` | The extract is not yet in a terminal state (e.g. `ACCEPTED` / `RUNNING`). Poll status first. |
| `410 Gone` | The extract existed and completed, but its files have been removed per retention policy (status `EXPIRED`, see §5.5). This is a **terminal** state — consumers replaying an old `extract.ready` event MUST NOT treat it as retryable. Re-submit a fresh extract request if the data is still required. |

#### 3.2.4 Cancel Extract

```
POST /api/v1/extracts/{extract_id}/cancel
```

**Purpose:** Cancel an in-progress extract. Idempotent — cancelling an already-cancelled or completed extract returns 200.

**Response — 200 OK:**

```json
{
  "extract_id": "ext_20260103_041722_a7f3",
  "status": "CANCELLED",
  "cancelled_at": "2026-01-03T04:19:30Z",
  "cancelled_by": "fes-plus-plus"
}
```

**Notes:**
- Partial files from a cancelled run are cleaned up asynchronously. No event is published for cancelled runs.
- If the extract has already reached `COMPLETED`, cancellation is treated as a signal that the consumer is done and files may be cleaned up early (before the standard retention window).

#### 3.2.5 List Extracts (History)

```
GET /api/v1/extracts?domain=nav-ledger&status=COMPLETED&limit=20&cursor=...
```

**Purpose:** Paginated listing of extract requests visible to the calling application. Supports filtering by domain, status, date range.

**Query Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `domain` | string | Filter by business domain. |
| `status` | enum | Filter by status. Comma-separated for multiple. |
| `created_after` | datetime | Only extracts created after this timestamp. |
| `created_before` | datetime | Only extracts created before this timestamp. |
| `fund_id` | string | Filter to extracts that include this fund. |
| `limit` | integer | Page size. Default 20, max 100. |
| `cursor` | string | Opaque pagination cursor from previous response. |

---

## 4. Event Bus Contract

### 4.1 Topic Naming Convention

```
{namespace}.{domain}.extract.{event_type}
```

**Examples:**
- `fund-services.nav-ledger.extract.ready`
- `fund-services.nav-ledger.extract.failed`
- `fund-services.gl.extract.ready`

### 4.2 Consumer Group Convention

```
{consuming_app_id}.{domain}.extract
```

Each consuming application registers its own consumer group. This ensures independent offset tracking, independent failure isolation, and independent replay capability.

### 4.3 Event Schema — `extract.ready`

Published when an extract completes successfully and all files are available in the object store.

```json
{
  "event_id": "evt_20260103_042248_x9k2",
  "event_type": "extract.ready",
  "event_version": "1.0",
  "emitted_at": "2026-01-03T04:22:48Z",
  "source": "extract-api",

  "extract_id": "ext_20260103_041722_a7f3",
  "idempotency_key": "fes-yearly-2025-run-001",

  "scope": {
    "domain": "nav-ledger",
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "as_of": "2026-01-02T23:59:59Z",
    "fund_scope": ["fund_A", "fund_B", "fund_C"],
    "frequency": "yearly"
  },

  "artifact": {
    "file_count": 3,
    "total_bytes": 184320000,
    "total_rows": 2847103,
    "format": "parquet",
    "compression": "gzip",
    "checksum": "sha256:9f4b2c...",
    "schema_version": "nav-ledger/v3",
    "files_endpoint": "/api/v1/extracts/ext_20260103_041722_a7f3/files",
    "expires_at": "2026-01-10T04:22:48Z"
  },

  "lineage": {
    "run_id": "run_20260103_041722",
    "sources": ["ods.nav_positions", "warehouse.gl_entries"],
    "produced_by": "extract-worker",
    "duration_seconds": 326
  },

  "requester": {
    "app_id": "fes-plus-plus",
    "correlation_id": "corr-20260103-0417"
  }
}
```

**Design Notes:**
- The event contains the `files_endpoint` (API path), **not** direct storage URLs. This forces consumers to authenticate and go through the entitlement layer to obtain presigned URLs. No data leaks through the bus.
- `event_id` is globally unique and can be used for deduplication on the consumer side.
- `idempotency_key` is propagated so consumers can correlate events to their own request lifecycle.
- **Requester visibility.** `requester.app_id` and `requester.correlation_id` are present for end-to-end auditability. Because events are fanned out to multiple consumer groups, the originating `app_id` is visible to every consumer entitled to the domain. Deployments with strict information-disclosure requirements MAY redact `requester.app_id` to the constant `"internal"` on the fan-out path while preserving the true value in the audit log; `correlation_id` SHOULD be preserved so trace correlation still works. The redaction decision MUST be domain-registry-local (App. A) so that consumer expectations are stable.

### 4.4 Event Schema — `extract.failed`

Published when an extract fails after all retry attempts are exhausted.

```json
{
  "event_id": "evt_20260103_042530_f1m4",
  "event_type": "extract.failed",
  "event_version": "1.0",
  "emitted_at": "2026-01-03T04:25:30Z",
  "source": "extract-api",

  "extract_id": "ext_20260103_041722_a7f3",
  "idempotency_key": "fes-yearly-2025-run-001",

  "scope": {
    "domain": "nav-ledger",
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "as_of": "2026-01-02T23:59:59Z",
    "fund_scope": ["fund_A", "fund_B", "fund_C"],
    "frequency": "yearly"
  },

  "error": {
    "code": "SOURCE_TIMEOUT",
    "message": "Warehouse query exceeded 600s timeout for fund_B",
    "retries_attempted": 3,
    "partial_result": {
      "funds_completed": ["fund_A"],
      "funds_failed": ["fund_B", "fund_C"]
    }
  },

  "requester": {
    "app_id": "fes-plus-plus",
    "correlation_id": "corr-20260103-0417"
  }
}
```

### 4.5 Event Schema — `extract.expiring`

Published 24 hours before an extract's files are removed per retention policy. Gives consumers a final window to re-fetch if needed.

```json
{
  "event_id": "evt_20260109_042248_w3p7",
  "event_type": "extract.expiring",
  "event_version": "1.1",
  "emitted_at": "2026-01-09T04:22:48Z",
  "source": "extract-api",

  "extract_id": "ext_20260103_041722_a7f3",
  "idempotency_key": "fes-yearly-2025-run-001",

  "scope": {
    "domain": "nav-ledger",
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "as_of": "2026-01-02T23:59:59Z",
    "fund_scope": ["fund_A", "fund_B", "fund_C"],
    "frequency": "yearly"
  },

  "expires_at": "2026-01-10T04:22:48Z",
  "files_endpoint": "/api/v1/extracts/ext_20260103_041722_a7f3/files",

  "requester": {
    "app_id": "fes-plus-plus",
    "correlation_id": "corr-20260103-0417"
  }
}
```

**Schema consistency.** `extract.expiring` in v1.0 was missing the
`scope`, `idempotency_key`, and `requester` blocks that the other
extract events carry. v1.1 restores that symmetry so consumer libraries
can treat all three event types uniformly (same prefix, same routing
key derivation, same audit fields). `event_version` is bumped to `1.1`
for this event only — consumers built against `1.0` will continue to
deserialise correctly under the additive-evolution rule (§10.2) because
the new fields are strictly additional.

### 4.6 Event Delivery Guarantees

| Property | Guarantee |
|---|---|
| Delivery | At least once. Consumers must be idempotent. |
| Ordering | Per-partition ordering by `extract_id`. No global order guarantee. |
| Retention | Events retained on the bus for 7 days. Consumers that fall behind beyond 7 days must use the REST API to recover state. |
| Dead letter | Events that fail delivery after 5 attempts are routed to a dead-letter topic: `{topic}.dlq`. Operations is alerted. |
| Schema registry | All event schemas are registered in the schema registry with backward compatibility enforcement. |

### 4.7 Webhook Notification (Alternative to Event Bus)

For consumers that cannot subscribe to the event bus (external vendors, legacy systems), the API supports HTTP webhook callbacks.

**Webhook Delivery:**

```
POST {callback_url}
Content-Type: application/json
X-Extract-Signature: sha256=...
X-Extract-Event: extract.ready
X-Extract-Delivery: del_20260103_042248_r8s1
```

The body is identical to the `extract.ready` event schema (§4.3).

**Webhook Contract:**
- The API expects a `2xx` response within **10 seconds**.
- On failure: retry with exponential backoff (10s, 30s, 90s, 270s, 810s) up to **5 attempts**.
- After 5 failures: the webhook is marked as `FAILED_DELIVERY` on the extract status. The consumer can still poll via the REST API.
- `X-Extract-Signature` is an HMAC-SHA256 of the body using a pre-shared secret registered during app onboarding. Consumers must verify this before processing.
- The signature is computed over the raw request body bytes (not the parsed JSON) concatenated with the `X-Extract-Delivery` and `X-Extract-Timestamp` header values in a canonical format: `HMAC-SHA256(secret, timestamp + "." + delivery_id + "." + body)`. Consumers MUST reject requests where `X-Extract-Timestamp` differs from their clock by more than **5 minutes** to mitigate replay attacks.

#### 4.7.1 Secret Rotation

Pre-shared secrets are rotated on a **90-day cadence** (or on demand
after a suspected compromise). Rotation uses an **overlap window**
rather than a hard cutover — the same pattern GitHub and Stripe use
for their webhook secrets:

1. **Announce.** Operations issues a new secret `s_new` alongside the
   existing `s_old` and notifies the consumer (API surface:
   `PUT /api/v1/apps/{app_id}/webhook-secret`, returns both values).
2. **Overlap (T + 0 to T + 14 days).** The API signs each outbound
   delivery **twice**, emitting both `X-Extract-Signature` (computed
   with `s_new`) and `X-Extract-Signature-Legacy` (computed with
   `s_old`). Consumers accept a delivery if **either** header
   verifies. During this window the consumer can cut over on its own
   schedule by switching the secret it verifies against.
3. **Retire (T + 14 days).** Operations confirms that no deliveries
   in the last 48 hours used the legacy signature by querying the
   audit log; Operations then removes `s_old` server-side. The API
   stops emitting `X-Extract-Signature-Legacy`.

**Compromise fast-path.** On a suspected compromise, the overlap
window is compressed to **1 hour** and the consumer is paged; the
API will begin issuing `410 Gone` on the webhook-registration
endpoint for the compromised secret until a new one is provisioned.

**Operational invariant.** At any point in time, the API has **at
most two active secrets per app_id** — a current one and a legacy
one. The legacy one is purged after the overlap window closes.

---

## 5. Object Storage Contract

### 5.1 Path Convention

```
/extracts/{domain}/{yyyy}/{mm}/{extract_id}/
    ├── {partition_key}.{format}.{compression}
    ├── {partition_key}.{format}.{compression}
    ├── ...
    ├── manifest.json
    └── _SUCCESS
```

**Example:**

```
/extracts/nav-ledger/2026/01/ext_20260103_041722_a7f3/
    ├── fund_A.parquet.gz
    ├── fund_B.parquet.gz
    ├── fund_C.parquet.gz
    ├── manifest.json
    └── _SUCCESS
```

### 5.2 File Sizing Guidelines

| Guideline | Value | Rationale |
|---|---|---|
| Target file size | 100–250 MB compressed | Optimal for parallel ingestion by downstream warehouses and processing engines. |
| Minimum file size | 10 MB | Below this, per-file overhead dominates. Combine small funds into a single file. |
| Maximum file size | 5 GB | Above this, split by date sub-partition within the fund. |
| Compression | gzip (default) or zstd | zstd offers better compression ratio and decompression speed but has less universal tooling support. |

### 5.3 Manifest File

Every extract run produces a `manifest.json` that serves as the authoritative metadata record.

```json
{
  "manifest_version": "1.0",
  "extract_id": "ext_20260103_041722_a7f3",
  "run_id": "run_20260103_041722",
  "domain": "nav-ledger",
  "period": {
    "start": "2025-01-01",
    "end": "2025-12-31"
  },
  "as_of": "2026-01-02T23:59:59Z",
  "schema_version": "nav-ledger/v3",
  "created_at": "2026-01-03T04:22:48Z",
  "expires_at": "2026-01-10T04:22:48Z",

  "files": [
    {
      "name": "fund_A.parquet.gz",
      "partition_key": "fund_A",
      "size_bytes": 62140000,
      "row_count": 948201,
      "checksum": "sha256:a1b2c3..."
    },
    {
      "name": "fund_B.parquet.gz",
      "partition_key": "fund_B",
      "size_bytes": 58920000,
      "row_count": 903412,
      "checksum": "sha256:d4e5f6..."
    },
    {
      "name": "fund_C.parquet.gz",
      "partition_key": "fund_C",
      "size_bytes": 63260000,
      "row_count": 995490,
      "checksum": "sha256:g7h8i9..."
    }
  ],

  "totals": {
    "file_count": 3,
    "total_bytes": 184320000,
    "total_rows": 2847103
  },

  "lineage": {
    "sources": ["ods.nav_positions", "warehouse.gl_entries"],
    "query_fingerprint": "sha256:j0k1l2...",
    "worker_version": "extract-worker/2.4.1"
  }
}
```

**Note.** The `_SUCCESS` marker (see §5.4) is **not** listed in
`manifest.files[]`. It is a publish-protocol artefact with no
checksum and no business meaning; checksum-verifying consumers would
otherwise flag it as an unexpected extra file.

### 5.4 Atomic Publish Protocol

Files are written to a staging prefix first, then atomically promoted:

```
1. Worker writes files to:    /extracts/.staging/{extract_id}/
2. Worker validates:          checksums, row counts, schema conformance
3. Worker renames prefix to:  /extracts/{domain}/{yyyy}/{mm}/{extract_id}/
4. Worker writes:             _SUCCESS marker
5. Worker publishes:          extract.ready event
```

- The `_SUCCESS` marker is the atomic signal. Consumers must check for its existence before reading files.
- If the worker crashes between steps 3 and 5, the recovery process detects orphaned directories (present but no event) and either re-publishes the event or cleans up, depending on file validation.

### 5.5 Retention Policy

| Category | Retention | Action at Expiry |
|---|---|---|
| Standard extract | 7 calendar days | Files deleted. Status moves to `EXPIRED`. `extract.expiring` event fires 24h before. |
| Regulatory extract | 90 calendar days | Configurable per domain. Overridden by request parameter `retention_days`. |
| Cancelled extract | 0 (immediate) | Staging files cleaned up. No published files exist. |
| Manifest metadata | Indefinite | Manifest record retained in the data catalog for lineage. Files deleted per above. |

#### 5.5.1 Retention / In-Flight Download Invariant

A consumer may be mid-download when retention fires. The following
invariant MUST hold:

> **Every presigned URL's hard expiry time is strictly less than the
> `expires_at` of the underlying extract.** File deletion is scheduled
> at `expires_at`; URLs issued at `expires_at - Δ` MUST have a
> `url_expires_at` no later than `expires_at - 60s`.

In plain terms: **a URL stops being valid before the file it points at
is deleted**, so a consumer never gets a mid-stream read error caused
by the retention sweep. The sweep itself is a two-phase operation:

1. **Phase 1 — quiesce.** Mark the extract as `EXPIRING` in storage
   metadata and stop issuing new presigned URLs (`GET /files` returns
   `410 Gone` from this point). In-flight downloads continue until
   their URL expires naturally.
2. **Phase 2 — delete.** After a quiet period of `url_ttl + safety_margin`
   (default: **1 hour + 5 minutes**), the sweep deletes the files from
   the object store and transitions the extract to `EXPIRED`. By
   construction, no valid URL can reference the files at this point.

Implementations that delegate retention to object-store lifecycle
rules (S3 lifecycle, GCS object lifecycle) MUST configure the rule to
fire no earlier than `expires_at + url_ttl + safety_margin` to preserve
the same invariant.

---

## 6. Security Model

### 6.1 Authentication

| Channel | Method |
|---|---|
| REST API | Mutual TLS (mTLS) + OAuth 2.0 client credentials. Every request must present a valid access token issued by the identity provider. |
| Event Bus | SASL/SCRAM or mTLS, depending on broker configuration. Consumer group ACLs are enforced. |
| Object Store | Presigned URLs issued by the API. No direct credential access to storage. |
| Webhook | HMAC-SHA256 signature verification using a pre-shared secret. |

### 6.2 Authorisation & Entitlements

Entitlements are enforced at the **API tier** using a fund-level access control list (ACL).

| Rule | Enforcement Point |
|---|---|
| An application can only request extracts for funds it is entitled to. | `POST /extracts` — 403 if `fund_scope` includes non-entitled funds. |
| An application can only see its own extract requests. | `GET /extracts` — filtered by `requester.app_id`. |
| Presigned URLs are scoped to entitled funds only. | `GET /extracts/{id}/files` — files for non-entitled funds are omitted. |
| Events contain fund_scope but not file URLs. | Consumers must authenticate to obtain URLs, ensuring entitlement check occurs. |

### 6.3 Encryption

| Layer | Standard |
|---|---|
| In transit (API) | TLS 1.2+ with strong cipher suites. mTLS for service-to-service. |
| In transit (Bus) | TLS 1.2+ for broker connections. |
| At rest (Object Store) | AES-256 server-side encryption. Customer-managed keys (CMK) for regulated domains. |
| At rest (Bus) | Broker-level encryption at rest. Events do not contain PII or payload data. |

### 6.4 Audit Trail

Every API call, event publication, and file access is logged to an immutable audit store with:

- Timestamp, actor (app_id), action, resource (extract_id), outcome (success/failure)
- Source IP, mTLS certificate fingerprint
- For file downloads: file_id, size, checksum, download timestamp

Audit logs are retained for a minimum of 7 years (regulatory requirement).

### 6.5 Key Management

Server-side encryption at rest (§6.3) is necessary but not sufficient
in a regulated multi-tenant estate — it protects only against the
storage operator, not against compromise of a consumer bearer token
or an API-tier bug that widens the entitlement boundary. The
following additional controls apply:

| Control | Detail |
|---|---|
| **Per-tenant KMS keys (CMK)** | Every `tenant_id` (typically = fund manager) is associated with a dedicated KMS key. Extract files written for that tenant are encrypted under that key. A compromised API instance cannot decrypt another tenant's files without that tenant's KMS grant. |
| **Envelope encryption on the manifest** | The `manifest.json` is encrypted with a per-extract data key, which is itself wrapped with the tenant CMK. The wrapped key is stored in a `manifest.json.dek` sidecar. Consumers obtain the unwrapped DEK by calling the API (which holds the KMS decrypt grant), never by calling KMS directly. |
| **Webhook / HMAC secrets** | Stored in a secrets manager, never in the primary database. Rotated per §4.7.1. |
| **Presigned URL signing key** | Separate from the tenant CMKs. Rotated independently on a 30-day cadence. |
| **Key rotation cadence** | CMKs rotated annually (automatic); signing keys 30 days; HMAC secrets 90 days; on-demand rotation on any suspected compromise. |
| **BYOK option** | For regulated domains (e.g. where a tenant is contractually required to hold the key material), the tenant may bring their own CMK. The API will use the tenant-supplied key for all writes; a failure to access the tenant key puts extracts for that tenant into `FAILED` rather than allowing unencrypted writes. |

The bearer token is not the only line of defence: an attacker with a
stolen token still cannot read another tenant's files because the
object-store objects are encrypted under that tenant's CMK and the
API enforces the `tenant_id → CMK` mapping at download time.

---

## 7. Error Handling & Retry Semantics

### 7.1 Error Taxonomy

| Error Code | Category | Retryable? | Description |
|---|---|---|---|
| `VALIDATION_ERROR` | Client | No | Request body fails schema or business validation. |
| `ENTITLEMENT_DENIED` | Client | No | Requesting app not entitled to one or more funds. |
| `RATE_LIMITED` | Client | Yes | Too many requests. Honour `Retry-After`. |
| `SOURCE_TIMEOUT` | Server | Yes | Warehouse or ODS query exceeded timeout. |
| `SOURCE_UNAVAILABLE` | Server | Yes | Source system is unreachable. |
| `STORAGE_WRITE_FAILED` | Server | Yes | Object store write failed. |
| `PARTIAL_FAILURE` | Server | Partial | Some funds extracted successfully, others failed. |
| `SCHEMA_MISMATCH` | Server | No | Source data does not conform to expected schema version. |
| `INTERNAL_ERROR` | Server | Yes | Unclassified server error. |

### 7.2 Worker Retry Policy

```
Retry strategy:  Exponential backoff with jitter
Base delay:      5 seconds
Max delay:       300 seconds (5 minutes)
Max attempts:    3 per fund, 5 per extract
Jitter:          ±20% of calculated delay

On final failure:
  1. Publish extract.failed event
  2. Set extract status to FAILED
  3. Attach error details with per-fund breakdown
  4. Alert operations via monitoring channel
```

### 7.3 Partial Success

`PARTIAL` is a **first-class terminal status** (§3.2.2), not a
sub-type of `FAILED`. The distinction matters operationally: a
fund-services consumer can usually reconcile against the funds that
succeeded and re-submit a narrower request for the rest, whereas a
`FAILED` extract has no usable output. Runbook procedures, SLA
accounting, and dashboards treat them as separate outcomes.

If some funds in a multi-fund extract succeed and others fail after
exhausting retries:

- Status is set to `PARTIAL` (terminal).
- Successfully extracted files are published and available via
  `GET /extracts/{id}/files` exactly as for a `COMPLETED` extract.
- The `manifest.json` lists only the successful funds.
- The worker emits an `extract.ready` event (for the completed
  portion — consumers downstream of the successful funds must not be
  blocked) **and** a separate `extract.failed` event whose `error`
  block contains `code: PARTIAL_FAILURE` and a `partial_result`
  object with `funds_completed` and `funds_failed`. Consumers that
  care about completeness MUST correlate the two events by
  `extract_id`.
- The consumer decides whether to use partial results or wait for a
  resubmission. Re-submission uses a **new** `idempotency_key`
  (typically `{original-key}-retry-N`) because the parameters
  differ (`fund_scope` is narrowed to the failed set).

This keeps the success-event fast path decoupled from the failure
channel: a consumer interested only in successfully-delivered data
can ignore `extract.failed` entirely.

### 7.4 Idempotency Contract

- `idempotency_key` is unique per logical request. If a second `POST /extracts` arrives with the same key and identical parameters, the API returns the existing extract resource (same `extract_id`, current status) with `200 OK` instead of `202 Accepted`.
- If the key matches but parameters differ, the API returns `409 Conflict`.
- Idempotency keys expire after 30 days.
- Event consumers use `event_id` for deduplication. Processing the same `event_id` twice must produce the same outcome.

---

## 8. SLA Framework

### 8.1 Service Level Indicators (SLIs)

| SLI | Definition | Measurement |
|---|---|---|
| **API Availability** | Percentage of successful (non-5xx) responses to valid requests | 1-minute windows, 30-day rolling |
| **Extract Completion Latency** | Time from `ACCEPTED` to `COMPLETED` status | Per-extract, p50/p95/p99 |
| **Event Delivery Latency** | Time from file publish to event arriving on consumer partition | Per-event, p50/p95/p99 |
| **File Availability** | Percentage of time completed extract files are downloadable within the retention window | 5-minute checks |
| **Throughput** | Number of concurrent extracts the system can process without degradation | Measured at peak |
| **Data Freshness** | Lag between source system commit and extract availability | Per-domain |

### 8.2 Service Level Objectives (SLOs)

| SLO | Target | Measurement Window |
|---|---|---|
| API Availability | 99.9% | 30-day rolling |
| Extract Completion (single fund, 1 year, < 1M rows) | p95 < 5 minutes | 30-day rolling |
| Extract Completion (multi-fund, 1 year, < 10M rows) | p95 < 15 minutes | 30-day rolling |
| Extract Completion (large, > 10M rows) | p95 < 45 minutes | 30-day rolling |
| Event Delivery Latency | p99 < 10 seconds after file publish | 30-day rolling |
| File Availability | 99.95% within retention window | 30-day rolling |
| Peak Throughput | 50 concurrent extracts without SLO degradation | Quarterly load test |
| Data Freshness (warehouse source) | < 4 hours from source commit | Per-domain |
| Data Freshness (ODS source) | < 30 minutes from source commit | Per-domain |

### 8.3 Rate Limits

| Limit | Value | Scope |
|---|---|---|
| Request rate (POST /extracts) | 60 requests/minute | Per app_id |
| **Status polling (GET /extracts/{id})** — app uses `event` or `webhook` notification | 60 polls/minute | Per app_id |
| **Status polling — app uses `notification.mode = none`** | **10 polls/minute** | Per app_id |
| List extracts (GET /extracts) | 30 requests/minute | Per app_id |
| Concurrent active extracts | 10 | Per app_id |
| Maximum period span | 5 years | Per request |
| Maximum fund_scope size | 500 funds | Per request |
| Presigned URL generation | 120 calls/minute | Per app_id |

The stricter polling limit for `notification.mode = none` is
deliberate: status polling is a recovery mechanism, not the primary
completion signal. Apps that need low-latency completion MUST use the
bus or webhooks (§3.2.1, §3.2.2).

Rate limit responses include:
- `429 Too Many Requests` status
- `Retry-After` header with seconds until the limit resets
- `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers on every response

### 8.4 Peak Window Capacity Planning

The January year-end window is the design-driving peak. Capacity must be provisioned for:

```
Peak model:
  Yearly distributing funds:     ~200 funds
  Quarterly funds (Q4 close):    ~800 funds
  Monthly funds (Dec close):     ~3000 funds

  Estimated concurrent requests:  50–80
  Estimated total rows:           ~50M across all extracts
  Estimated storage:              ~25 GB compressed

  Worker pool sizing:             auto-scale to 20 workers
  Warehouse compute:              dedicated extract warehouse, XL size
  Target completion:              all year-end extracts within 4 hours
```

#### 8.4.1 Worker Scale-Out Mechanism

The reference implementation runs a single-process `asyncio` task
pool — adequate for a contract executable, but explicitly **not** the
production topology. Scale-out to N workers requires a distribution
mechanism that guarantees **exactly one worker per extract** without a
distributed lock in the hot path. The canonical options:

| Mechanism | Where it fits |
|---|---|
| **Postgres `SELECT ... FOR UPDATE SKIP LOCKED`** | Default choice. Workers poll the `extracts` table for `status = ACCEPTED`, lock a row with `SKIP LOCKED`, set it to `RUNNING`, and commit. The `SKIP LOCKED` clause means contention between workers resolves at the storage layer without a separate lock service. Suits up to ~100 workers / ~1k req/min comfortably. |
| **Kafka consumer groups with a work-assignment topic** | Producer writes one message per extract to a `extracts.work` topic partitioned by `extract_id`. Workers form a consumer group; the broker handles partition assignment. Fits estates that already operate Kafka and want one less database touchpoint. Requires care around rebalancing during long-running extracts (static group membership or cooperative rebalancing). |
| **K8s Jobs one-per-extract** | The API creates a `Job` resource per extract; the scheduler handles placement and retries. Highest isolation, highest per-job overhead, right for long-running regulatory extracts measured in hours. |

The worker interface (`extract_service/worker.py`) is deliberately
narrow — `enqueue(request) → extract_id` and `poll_next() → job` —
so any of the three can be substituted without changing the API
layer. The **only invariant** across implementations is: between
`status = RUNNING` and `status = COMPLETED | PARTIAL | FAILED`, at
most one process is advancing the extract. `SKIP LOCKED` satisfies
this at the row level; Kafka satisfies it at the partition level; K8s
satisfies it at the job level.

Auto-scale signals (queue depth, p95 latency) are the same across
mechanisms; the specifics of how the worker pool grows are an
operational concern, not a contract concern.

---

## 9. Observability

### 9.1 Structured Logging

All components emit structured JSON logs with the following mandatory fields:

| Field | Description |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `level` | `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `service` | Component name (e.g., `extract-api`, `extract-worker`) |
| `extract_id` | Present on all extract-related log lines |
| `run_id` | Worker-specific execution identifier |
| `app_id` | Requesting application |
| `correlation_id` | End-to-end trace ID |
| `fund_id` | Present on per-fund operations |
| `duration_ms` | Elapsed time for the logged operation |

### 9.2 Metrics

| Metric | Type | Labels |
|---|---|---|
| `extract.requests.total` | Counter | `domain`, `status`, `app_id` |
| `extract.completion.duration_seconds` | Histogram | `domain`, `fund_count_bucket` |
| `extract.files.bytes_written` | Counter | `domain`, `format` |
| `extract.events.published` | Counter | `event_type`, `domain` |
| `extract.events.delivery_latency_seconds` | Histogram | `topic`, `consumer_group` |
| `extract.worker.queue_depth` | Gauge | `priority` |
| `extract.worker.active` | Gauge | — |
| `extract.api.request_latency_seconds` | Histogram | `endpoint`, `status_code` |
| `extract.storage.presigned_urls_issued` | Counter | `app_id` |

### 9.3 Alerts

| Alert | Condition | Severity | Action |
|---|---|---|---|
| High queue depth | Worker queue > 30 for > 5 min | Warning | Auto-scale workers. Notify on-call. |
| Extract failure rate | > 5% of extracts fail in 15 min window | Critical | Page on-call. Check source availability. |
| Event delivery lag | Consumer lag > 1000 events for > 10 min | Warning | Check consumer health. |
| Dead letter queue growth | DLQ depth > 0 | Warning | Investigate and reprocess. |
| Storage write errors | > 3 write failures in 5 min | Critical | Check storage quotas and connectivity. |
| API error rate | 5xx rate > 1% in 5 min window | Critical | Page on-call. |
| Approaching rate limit | App at > 80% of rate limit | Info | Notify app team. |

### 9.4 Distributed Tracing

Every request carries a `correlation_id` (client-provided or
API-generated) that propagates through:

```
Client request → API → Worker → Storage write → Event publish → Consumer receipt
```

Trace spans are emitted at each boundary, enabling end-to-end latency
breakdown in the tracing backend.

#### 9.4.1 Correlation-ID Enforcement

The correlation-id propagation chain above is five hops and six
opportunities to drop the value. This is the contract:

1. **Clients SHOULD provide `requester.correlation_id`** on
   `POST /extracts` (and may provide it as a `X-Correlation-ID`
   request header instead — the header wins if both are present).
2. **If the client does not provide one, the API MUST mint one**
   using `corr_{yyyymmdd}_{8-char-random}` and echo it back in the
   `X-Correlation-ID` response header and in the `links.self`
   resource.
3. **Every downstream hop MUST propagate the value unchanged** into
   its logs, events, and outbound HTTP calls. Webhooks include an
   `X-Correlation-ID` header; events carry
   `requester.correlation_id`.
4. **A missing `correlation_id` on a log line, event, or webhook
   delivery is a contract violation** and MUST fire the alert in
   §9.3 (`correlation_id_drop_rate > 0.1% over 15 min`). The
   mitigation is to identify and fix the upstream producer, not to
   drop the alert threshold.

Rationale: the audit log, the SLI measurements, and every runbook
start from a `correlation_id` search. If IDs are being lost, all three
degrade silently — so we treat the loss as a bug, not a best-effort
expectation.

---

## 10. Versioning & Evolution

### 10.1 API Versioning

- The API is versioned via the URL path: `/api/v1/extracts`.
- Breaking changes increment the major version (`v2`). Both versions run concurrently during a deprecation window of **6 months minimum**.
- Non-breaking changes (new optional fields, new status values, new query parameters) are introduced within the current version.

### 10.2 Event Schema Versioning

- `event_version` is included in every event (`"1.0"`).
- Schema evolution follows backward compatibility: consumers built against `1.0` must be able to deserialize `1.1` events (new fields are ignored by old consumers).
- Breaking changes produce a new topic (e.g., `extract.v2.ready`) and run in parallel during migration.

### 10.3 Output Schema Versioning

- The schema of the extracted data files (column names, types, semantics) is managed separately as a domain data contract.
- `schema_version` in the manifest and event allows consumers to handle multiple schema versions or reject unknown versions gracefully.
- Schema changes are announced via the data catalog and communicated 30 days before deployment.

### 10.4 Versioning Interaction Matrix

There are three independent version axes (API path, event schema,
data schema) and three different change-management windows (6 months
for API majors, parallel topic for event breaking changes, 30 days
for data schema). The principle that governs their composition is:

> **The most conservative window wins.** If a single release touches
> more than one axis, the effective deprecation window is the longest
> of the windows implied by the individual axes.

The concrete interactions:

| Breaking change in… | Consumer is still on… | Resolution | Effective window |
|---|---|---|---|
| API (`v1 → v2`) | API `v1` | Both API versions run in parallel at `/api/v1/` and `/api/v2/`. Events and data schemas unaffected. | **6 months** |
| Event schema (`extract.ready → extract.v2.ready`) | `extract.ready` | Both topics publish in parallel. Consumer groups cut over individually. API unaffected. | **parallel topic, no hard cutoff** (deprecation announced; removal requires all consumer groups to have moved) |
| Data schema (`nav-ledger/v3 → /v4`) | Processes data at `v3` | Consumer reads `schema_version` from the manifest and either upgrades or rejects. Producer publishes both versions during the window if demand justifies it. | **30 days** notice, then producer-only choice |
| API **and** event (e.g. `v2` adds a new required event field) | Both old | API `v1` continues to emit old events; API `v2` emits new events. The 6-month API window governs. | **6 months** (dominates) |
| Event **and** data (new event field is derived from a new data column) | Both old | Held to the longer of the two windows — in practice the 30-day data schema window is fastest, so the event change is held until the data change is also ready or is held open until it is. | **max(event window, 30 days)** |
| All three at once | Any old | Treat as a full major release. Use the **API 6-month window** as the outer bound; stage the event and data changes inside it. | **6 months** |

**Composition rules:**

1. Never gate two changes on each other across different windows —
   always sequence them so the longer window encloses the shorter one.
2. A breaking data schema change (30 days) inside an API v1→v2
   transition (6 months) MUST be announced against both current and
   next API versions. Consumers on `v1` get the same 30-day window as
   consumers on `v2`.
3. A consumer is considered "safe to move" only when it has
   successfully processed the new shape on the sandbox endpoint
   (`/sandbox/v1/extracts`) for at least 24 hours, and has committed
   an offset past the cutover point on the new topic (if applicable).
4. The producer keeps an internal compatibility matrix
   (`consumer_group × supported_versions`) and refuses to retire an
   old version while any group is still listed as supporting only
   it. This is an operational gate, not a contract gate.

---

## 11. Consumer Onboarding

### 11.1 Prerequisites

| Step | Owner | Deliverable |
|---|---|---|
| Register application in service registry | Consumer team | `app_id`, mTLS certificate |
| Request fund entitlements | Consumer team + data governance | Entitlement grant in ACL |
| Register consumer group on event bus | Platform team | Consumer group ID, topic ACL |
| Register webhook (if applicable) | Consumer team | Callback URL, shared secret |
| Validate in sandbox environment | Consumer team | Successful end-to-end test |
| Capacity review | Platform team + consumer team | Peak window impact assessment |

### 11.2 Sandbox Environment

A sandbox environment is available with:
- Synthetic fund data (not production)
- Full API contract parity
- Event bus with test topics
- 24-hour file retention
- No rate limits

Sandbox base path: `/sandbox/v1/extracts`

### 11.3 Consumer Responsibilities

| Responsibility | Detail |
|---|---|
| **Idempotent processing** | Handle duplicate events gracefully (use `event_id` for dedup). |
| **Offset management** | Commit offsets only after successful processing. |
| **Backpressure** | Do not consume faster than you can process. Use consumer group flow control. |
| **Error reporting** | Report processing failures back via a dedicated error topic or API, not by re-requesting the extract. |
| **Checksum verification** | Verify file checksums from the manifest before processing. |
| **Schema handling** | Handle unknown fields in events and data files without failure. |

---

## 12. Operational Runbook Hooks

This section defines the operational scenarios and the expected system behaviour. Full runbook procedures are maintained separately.

### 12.1 Scenario: Source System Outage

- **Detection:** Worker health check fails against source. `SOURCE_UNAVAILABLE` errors spike.
- **Behaviour:** Worker retries per §7.2. After max retries, publishes `extract.failed`. Queue drains naturally as new requests continue to fail fast.
- **Recovery:** When source recovers, failed extracts can be re-submitted by consumers using the original `idempotency_key` with a new suffix (e.g., `...-retry-1`).

### 12.2 Scenario: Event Bus Outage

- **Detection:** Event publish latency spikes. Dead-letter queue grows.
- **Behaviour:** Workers buffer events for up to 5 minutes. If the bus remains unavailable, extract status is still set to `COMPLETED` (files are available). Events are queued for retry.
- **Recovery:** Buffered events are published when the bus recovers. Consumers can also poll the REST API as a fallback — the API is the system of record, not the bus.

### 12.3 Scenario: Storage Outage

- **Detection:** `STORAGE_WRITE_FAILED` errors.
- **Behaviour:** Worker retries writes. If storage remains unavailable, extract fails. No partial files are left in published prefix (staging is cleaned up).
- **Recovery:** Re-submit extract.

### 12.4 Scenario: January Peak Overload

- **Detection:** Worker queue depth exceeds threshold. Completion latency SLO breached.
- **Behaviour:** Auto-scale worker pool. Priority queue ensures `high` priority extracts (year-end distributions) are processed before `normal` and `low`.
- **Manual intervention:** Operations can temporarily increase rate limits for critical app_ids and pause `low` priority jobs.

### 12.5 Scenario: Consumer Falls Behind

- **Detection:** Consumer lag metric exceeds threshold.
- **Behaviour:** Events remain on the bus for 7 days. Consumer can resume from its last committed offset.
- **Recovery:** If lag exceeds 7 days, consumer must use `GET /api/v1/extracts?created_after=...` to discover missed extracts and re-fetch files via the API. The API is always the recovery path.

---

## Appendix A: Domain Registry

| Domain | Description | Source Systems | Schema Doc | As-of semantics | Lifecycle state |
|---|---|---|---|---|---|
| `nav-ledger` | NAV positions and ledger entries | ODS (nav_positions), Warehouse (gl_entries) | `catalog://nav-ledger/v3` | Bi-temporal: `as_of` resolves to the warehouse `SYSTEM_TIME AS OF` clause on `gl_entries`; ODS contribution is last-write-wins (see A.2). | ACTIVE |
| `gl` | General ledger journal entries | Warehouse (gl_entries) | `catalog://gl/v2` | `SYSTEM_TIME AS OF`. | ACTIVE |
| `sub-ledger` | Sub-ledger detail (subscriptions, redemptions) | ODS (sub_ledger) | `catalog://sub-ledger/v1` | Snapshot-only — `as_of` returns the latest `commit_ts ≤ requested`. | ACTIVE |
| `transaction` | Trade and settlement transactions | ODS (transactions) | `catalog://transaction/v4` | Snapshot-only (as above). | ACTIVE |

New domains are registered through the data governance process and require schema publication in the data catalog before the extract API will accept requests.

### A.1 Domain Lifecycle

Domains have a lifecycle. The API's behaviour depends on the state:

| State | API behaviour | Typical duration |
|---|---|---|
| **`PROPOSED`** | Visible in `GET /api/v1/domains` with `state = PROPOSED` and a target activation date. `POST /extracts` against the domain returns `400 Bad Request` with `code: DOMAIN_NOT_ACTIVE`. No schema is registered yet; this state is for governance tracking only. | Days to weeks |
| **`ACTIVE`** | Fully operational. `POST /extracts` accepted; events published; schema registered in the catalog. | Indefinite |
| **`DEPRECATED`** | New requests return `400` with `code: DOMAIN_DEPRECATED` and a `successor_domain` in the error body. **In-flight extracts complete normally.** Existing consumer groups continue receiving events for already-completed runs. Deprecation is always paired with a migration notice to consumers. | 6 months minimum before RETIRED |
| **`RETIRED`** | The API rejects all operations against the domain with `410 Gone`. Historical files may still exist in the object store (subject to retention) but are not served via the API. The domain remains in the registry for audit-log interpretability. | Indefinite |

**Lifecycle transitions** are recorded in the audit log with the actor
(typically `data-governance`), the effective date, and a reference to
the governance ticket. Transitions that shorten a previously-announced
notice window require a documented regulatory or security reason.

### A.2 `as_of` Semantics Are Domain-Local

The `as_of` request parameter (§3.2.1) is **not** a globally-comparable
point-in-time. Each domain interprets it against the capabilities of
its source systems:

| Source capability | Interpretation | Consumer implication |
|---|---|---|
| **Bi-temporal tables** (warehouse `SYSTEM_VERSIONING`, bi-temporal DB) | Exact point-in-time reconstruction. Two extracts with the same `as_of` return identical data. | Cross-domain joins are valid. |
| **Snapshot / CDC log** (ODS, Kafka-log-backed) | Resolved to the latest `commit_ts ≤ as_of`. Data is point-in-time **as observed by the source**. Sub-second drift is possible. | Cross-domain joins are approximate; use the `run_id` in manifests for forensic work. |
| **Opaque source** (Eagle, Geneva, InvestOne cases where `as_of` cannot be honoured) | The worker rejects the request with `code: AS_OF_NOT_SUPPORTED` and a domain-specific explanation. The consumer either omits `as_of` (taking "latest available") or submits a separate extract per domain. | `as_of` must be omitted. |

The manifest's `lineage.as_of` records the **resolved** point-in-time
for each source (not the requested value), and the domain registry
MUST document which interpretation a given domain uses. This is a
spec requirement for adding a new domain — a domain whose `as_of`
semantics are undocumented is considered incomplete and MAY NOT
transition from `PROPOSED` to `ACTIVE`.

**Consumer warning.** Two consumers asking for `as_of = 2026-01-02T23:59:59Z`
against `nav-ledger` and `sub-ledger` will get *different* resolved
point-in-times — the bi-temporal warehouse can honour the request
exactly, but the ODS will snap to the latest commit at or before that
timestamp. Comparing the two datasets for reconciliation requires
reading the `lineage.as_of` per source, not the requested value.

---

## Appendix B: HTTP Status Code Reference

| Status | Usage in This API |
|---|---|
| `200 OK` | Successful GET, successful cancel, idempotent duplicate POST |
| `202 Accepted` | New extract request accepted and queued |
| `303 See Other` | (Future) Redirect from status endpoint to result on completion |
| `400 Bad Request` | Validation failure |
| `401 Unauthorized` | Missing or invalid auth |
| `403 Forbidden` | Entitlement denied |
| `404 Not Found` | Extract ID does not exist or is not visible to caller |
| `409 Conflict` | Idempotency key collision with different parameters |
| `429 Too Many Requests` | Rate limit exceeded |
| `500 Internal Server Error` | Unhandled server error |
| `503 Service Unavailable` | System overloaded or in maintenance. `Retry-After` included. |

---

## Appendix C: Glossary

| Term | Definition |
|---|---|
| **Extract** | A materialised snapshot of data for a given domain, period, and fund scope, written to immutable files in the object store. |
| **Run** | A single execution of the extract worker. One extract request produces one run. |
| **Manifest** | A JSON metadata file co-located with extract files containing checksums, row counts, schema version, and lineage. |
| **As-of** | A point-in-time qualifier. Determines which version of the data is used when the source supports temporal tables or versioned records. |
| **Idempotency key** | A client-provided string that uniquely identifies a logical request. Prevents duplicate processing on retry. |
| **Consumer group** | A named group of consumer instances that share offset tracking on a topic. Each group processes each event exactly once (at-least-once delivery with consumer-side dedup). |
| **Dead-letter topic** | A secondary topic where events are routed after exhausting delivery retries. Monitored by operations for manual intervention. |
| **Presigned URL** | A time-limited, authenticated URL that grants read access to a specific object in storage without exposing storage credentials. |
| **_SUCCESS marker** | A zero-byte file written to the extract directory after all data files are validated. Its presence signals that the extract is complete and safe to read. |
