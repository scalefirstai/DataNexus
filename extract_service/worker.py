"""Extract worker (§7.2, §5.4): asynchronous priority queue processor.

Materialises per-fund files, applies atomic publish, publishes events.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .config import get_settings
from .domains import get_domain
from .event_bus import EventBus
from .logging_config import set_log_context
from .metrics import get_metrics
from .models import (
    Compression,
    ErrorCode,
    EventArtifact,
    EventLineage,
    EventRequester,
    EventScope,
    ExtractFailedError,
    ExtractFailedEvent,
    ExtractReadyEvent,
    ExtractStatus,
    Frequency,
    OutputFormat,
    Priority,
    utcnow,
)
from .object_store import ObjectStore, WrittenFile
from .storage import Storage
from .webhooks import WebhookDelivery

log = logging.getLogger("extract.worker")

PRIORITY_WEIGHT = {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}


@dataclass(order=True)
class Job:
    priority: int
    enqueued_at: float
    extract_id: str
    source_outage: bool = False


class Worker:
    def __init__(
        self,
        storage: Storage,
        object_store: ObjectStore,
        event_bus: EventBus,
        webhook_delivery: WebhookDelivery,
    ) -> None:
        self.storage = storage
        self.object_store = object_store
        self.event_bus = event_bus
        self.webhook_delivery = webhook_delivery
        self._queue: asyncio.PriorityQueue[Job] = asyncio.PriorityQueue()
        self._workers: list[asyncio.Task] = []
        self._cancellations: set[str] = set()
        self._stop = asyncio.Event()
        self._simulate_source_outage = False
        self._simulate_partial_failure: dict[str, set[str]] = {}
        self._paused = asyncio.Event()
        self._paused.set()  # start unpaused

    # ---- admin hooks (used by tests / runbook scenarios) ----

    def simulate_source_outage(self, enabled: bool) -> None:
        self._simulate_source_outage = enabled

    def simulate_partial_failure(self, extract_id: str, fail_funds: set[str]) -> None:
        self._simulate_partial_failure[extract_id] = fail_funds

    def pause_processing(self) -> None:
        self._paused.clear()

    def resume_processing(self) -> None:
        self._paused.set()

    # ---- lifecycle ----

    async def start(self, concurrency: int | None = None) -> None:
        c = concurrency or get_settings().worker_concurrency
        for i in range(c):
            t = asyncio.create_task(self._run(), name=f"worker-{i}")
            self._workers.append(t)

    async def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            await self._queue.put(Job(priority=99, enqueued_at=0, extract_id="__STOP__"))
        for t in self._workers:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                t.cancel()
        self._workers.clear()

    def cancel(self, extract_id: str) -> None:
        self._cancellations.add(extract_id)

    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, extract_id: str, priority: Priority) -> None:
        job = Job(
            priority=PRIORITY_WEIGHT[priority],
            enqueued_at=utcnow().timestamp(),
            extract_id=extract_id,
        )
        await self._queue.put(job)
        get_metrics().set_gauge(
            "extract.worker.queue_depth",
            self._queue.qsize(),
            {"priority": priority.value},
        )

    # ---- processing loop ----

    async def _run(self) -> None:
        metrics = get_metrics()
        while not self._stop.is_set():
            await self._paused.wait()
            job = await self._queue.get()
            if job.extract_id == "__STOP__":
                return
            await self._paused.wait()
            metrics.set_gauge(
                "extract.worker.active", len([w for w in self._workers if not w.done()])
            )
            try:
                await self._process(job.extract_id)
            except Exception:
                log.exception("worker crashed for extract", extra={"extract_id": job.extract_id})

    async def _process(self, extract_id: str) -> None:
        row = self.storage.get_extract(extract_id)
        if not row:
            log.error("extract missing", extra={"extract_id": extract_id})
            return
        if row["status"] == ExtractStatus.CANCELLED.value:
            return

        request = row["request"]
        app_id = row["app_id"]
        domain = row["domain"]
        correlation_id = request.get("requester", {}).get("correlation_id")
        set_log_context(extract_id=extract_id, app_id=app_id, correlation_id=correlation_id)

        run_id = f"run_{utcnow().strftime('%Y%m%d_%H%M%S')}_{extract_id[-4:]}"
        self.storage.update_extract(
            extract_id,
            status=ExtractStatus.RUNNING,
            run_id=run_id,
            state_json={
                "progress": {
                    "funds_completed": 0,
                    "funds_total": len(request.get("fund_scope") or []),
                    "percent": 0,
                    "current_fund": None,
                }
            },
        )
        started = utcnow()

        fund_scope: list[str] = request.get("fund_scope") or ["fund_default"]
        funds_completed: list[str] = []
        funds_failed: list[str] = []
        files: list[WrittenFile] = []

        output = request["output"]
        fmt = OutputFormat(output["format"])
        compression = Compression(output.get("compression", "gzip"))

        per_extract_attempts = 0

        for fund in fund_scope:
            if extract_id in self._cancellations:
                log.info("cancellation observed mid-run", extra={"fund_id": fund})
                self._finalize_cancelled(extract_id)
                return

            set_log_context(fund_id=fund)
            success, retries = await self._extract_fund(
                extract_id, domain, fund, fmt, compression, request
            )
            per_extract_attempts += retries
            if success:
                funds_completed.append(fund)
                files.append(success)
            else:
                funds_failed.append(fund)
                if per_extract_attempts >= get_settings().worker_max_attempts_per_extract:
                    break

            percent = int(
                100 * (len(funds_completed) + len(funds_failed)) / max(1, len(fund_scope))
            )
            self.storage.update_extract(
                extract_id,
                state_json={
                    "progress": {
                        "funds_completed": len(funds_completed),
                        "funds_total": len(fund_scope),
                        "percent": percent,
                        "current_fund": fund,
                    }
                },
            )

        if not files:
            await self._finalize_failed(
                extract_id,
                run_id,
                funds_completed,
                funds_failed,
                (
                    ErrorCode.SOURCE_TIMEOUT
                    if self._simulate_source_outage
                    else ErrorCode.SOURCE_UNAVAILABLE
                ),
                "All partitions failed after retries",
                per_extract_attempts,
            )
            return

        if funds_failed:
            await self._finalize_partial(
                extract_id,
                run_id,
                funds_completed,
                funds_failed,
                files,
                started,
                per_extract_attempts,
            )
            return

        await self._finalize_completed(extract_id, run_id, files, started)

    # ---- per-fund extract with retry ----

    async def _extract_fund(
        self,
        extract_id: str,
        domain: str,
        fund: str,
        fmt: OutputFormat,
        compression: Compression,
        request: dict[str, Any],
    ) -> tuple[WrittenFile | None, int]:
        settings = get_settings()
        attempts = 0
        last_exc: Exception | None = None
        while attempts < settings.worker_max_attempts_per_fund:
            attempts += 1
            try:
                if self._simulate_source_outage or fund in self._simulate_partial_failure.get(
                    extract_id, set()
                ):
                    raise TimeoutError(f"simulated source failure for {fund}")
                rows = self._synthesize_rows(domain, fund, request)
                wf = self.object_store.write_partition(extract_id, fund, rows, fmt, compression)
                get_metrics().inc(
                    "extract.files.bytes_written",
                    wf.size_bytes,
                    {"domain": domain, "format": fmt.value},
                )
                return wf, attempts
            except Exception as e:
                last_exc = e
                delay = self._compute_backoff(attempts)
                log.warning(
                    "fund extract failed, backing off",
                    extra={
                        "fund_id": fund,
                        "attempt": attempts,
                        "delay_seconds": delay,
                        "error": str(e),
                    },
                )
                await asyncio.sleep(delay)
        log.error(
            "fund extract exhausted retries",
            extra={"fund_id": fund, "attempts": attempts, "error": str(last_exc)},
        )
        return None, attempts

    def _compute_backoff(self, attempt: int) -> float:
        settings = get_settings()
        base = settings.worker_retry_base_seconds * (2 ** (attempt - 1))
        capped = min(base, settings.worker_retry_max_seconds)
        jitter = capped * settings.worker_retry_jitter
        return max(0.01, capped + random.uniform(-jitter, jitter)) / 50.0
        # /50 keeps tests snappy; in production remove the divisor.

    # ---- row generation (stand-in for warehouse query) ----

    @staticmethod
    def _synthesize_rows(domain: str, fund: str, request: dict[str, Any]) -> list[dict[str, Any]]:
        import os

        period = request["period"]
        as_of = request.get("as_of")
        seed = int(hashlib.sha256(f"{domain}:{fund}".encode()).hexdigest(), 16) % 2000
        multiplier = int(os.environ.get("EXTRACT_ROWS_MULTIPLIER", "1"))
        row_count = (1000 + seed) * multiplier
        return [
            {
                "fund_id": fund,
                "period_start": period["start"],
                "period_end": period["end"],
                "as_of": as_of,
                "position_id": f"{fund}-pos-{i:08d}",
                "amount": round((i * 13.37) % 10000, 2),
                "currency": "USD",
                "description": f"synthetic ledger row for {fund} sequence {i:08d}",
            }
            for i in range(row_count)
        ]

    # ---- finalization ----

    def _finalize_cancelled(self, extract_id: str) -> None:
        self.object_store.cleanup_staging(extract_id)
        self.storage.update_extract(
            extract_id,
            status=ExtractStatus.CANCELLED,
            cancelled_at=utcnow(),
        )
        self._cancellations.discard(extract_id)

    async def _finalize_completed(
        self,
        extract_id: str,
        run_id: str,
        files: list[WrittenFile],
        started,
    ) -> None:
        row = self.storage.get_extract(extract_id)
        assert row
        request = row["request"]
        domain = row["domain"]
        schema_version = get_domain(domain).schema_version if get_domain(domain) else f"{domain}/v1"
        manifest, combined_checksum, total_bytes, total_rows = self._build_manifest(
            extract_id, run_id, files, request, schema_version
        )
        self.object_store.write_manifest(extract_id, manifest)
        self.object_store.promote(extract_id, domain)
        completed_at = utcnow()
        duration = int((completed_at - started).total_seconds())
        expires_at = completed_at + timedelta(
            days=request.get("retention_days") or get_settings().default_retention_days
        )

        state = {
            "files": [self._file_to_dict(f) for f in files],
            "manifest_checksum": combined_checksum,
            "total_bytes": total_bytes,
            "total_rows": total_rows,
            "schema_version": schema_version,
            "duration_seconds": duration,
        }
        self.storage.update_extract(
            extract_id,
            status=ExtractStatus.COMPLETED,
            state_json=state,
            completed_at=completed_at,
            expires_at=expires_at,
        )
        get_metrics().observe(
            "extract.completion.duration_seconds",
            duration,
            {"domain": domain, "fund_count_bucket": self._fund_bucket(len(files))},
        )
        get_metrics().inc(
            "extract.requests.total",
            labels={
                "domain": domain,
                "status": "COMPLETED",
                "app_id": row["app_id"],
            },
        )
        await self._emit_ready(extract_id, row, run_id, files, duration, expires_at, schema_version)

    async def _finalize_partial(
        self,
        extract_id: str,
        run_id: str,
        funds_completed: list[str],
        funds_failed: list[str],
        files: list[WrittenFile],
        started,
        retries_attempted: int,
    ) -> None:
        row = self.storage.get_extract(extract_id)
        assert row
        request = row["request"]
        domain = row["domain"]
        schema_version = get_domain(domain).schema_version if get_domain(domain) else f"{domain}/v1"
        manifest, combined_checksum, total_bytes, total_rows = self._build_manifest(
            extract_id, run_id, files, request, schema_version
        )
        self.object_store.write_manifest(extract_id, manifest)
        self.object_store.promote(extract_id, domain)
        completed_at = utcnow()
        duration = int((completed_at - started).total_seconds())
        expires_at = completed_at + timedelta(
            days=request.get("retention_days") or get_settings().default_retention_days
        )
        state = {
            "files": [self._file_to_dict(f) for f in files],
            "manifest_checksum": combined_checksum,
            "total_bytes": total_bytes,
            "total_rows": total_rows,
            "schema_version": schema_version,
            "duration_seconds": duration,
            "error": {
                "code": ErrorCode.PARTIAL_FAILURE.value,
                "message": "Some partitions failed",
                "retries_attempted": retries_attempted,
                "partial_result": {
                    "funds_completed": funds_completed,
                    "funds_failed": funds_failed,
                },
            },
        }
        self.storage.update_extract(
            extract_id,
            status=ExtractStatus.PARTIAL,
            state_json=state,
            completed_at=completed_at,
            expires_at=expires_at,
        )
        # Per §7.3 partial success publishes extract.failed with partial_result.
        await self._emit_failed(
            extract_id,
            row,
            ErrorCode.PARTIAL_FAILURE,
            "Some partitions failed",
            retries_attempted,
            {"funds_completed": funds_completed, "funds_failed": funds_failed},
        )
        get_metrics().inc(
            "extract.requests.total",
            labels={
                "domain": domain,
                "status": "PARTIAL",
                "app_id": row["app_id"],
            },
        )

    async def _finalize_failed(
        self,
        extract_id: str,
        run_id: str,
        funds_completed: list[str],
        funds_failed: list[str],
        code: ErrorCode,
        message: str,
        retries_attempted: int,
    ) -> None:
        row = self.storage.get_extract(extract_id)
        assert row
        self.object_store.cleanup_staging(extract_id)
        state = {
            "error": {
                "code": code.value,
                "message": message,
                "retries_attempted": retries_attempted,
                "partial_result": {
                    "funds_completed": funds_completed,
                    "funds_failed": funds_failed,
                },
            }
        }
        self.storage.update_extract(
            extract_id,
            status=ExtractStatus.FAILED,
            state_json=state,
        )
        await self._emit_failed(
            extract_id,
            row,
            code,
            message,
            retries_attempted,
            {"funds_completed": funds_completed, "funds_failed": funds_failed},
        )
        get_metrics().inc(
            "extract.requests.total",
            labels={
                "domain": row["domain"],
                "status": "FAILED",
                "app_id": row["app_id"],
            },
        )

    # ---- manifest + helpers ----

    @staticmethod
    def _file_to_dict(f: WrittenFile) -> dict[str, Any]:
        return {
            "partition_key": f.partition_key,
            "name": f.name,
            "size_bytes": f.size_bytes,
            "row_count": f.row_count,
            "checksum": f.checksum,
            "format": f.format.value,
            "compression": f.compression.value,
        }

    def _build_manifest(
        self,
        extract_id: str,
        run_id: str,
        files: list[WrittenFile],
        request: dict[str, Any],
        schema_version: str,
    ) -> tuple[dict[str, Any], str, int, int]:
        domain = request["domain"]
        domain_def = get_domain(domain)
        sources = list(domain_def.sources) if domain_def else []
        total_bytes = sum(f.size_bytes for f in files)
        total_rows = sum(f.row_count for f in files)
        combined = hashlib.sha256("".join(sorted(f.checksum for f in files)).encode()).hexdigest()
        manifest = {
            "manifest_version": "1.0",
            "extract_id": extract_id,
            "run_id": run_id,
            "domain": domain,
            "period": request["period"],
            "as_of": request.get("as_of"),
            "schema_version": schema_version,
            "created_at": utcnow().isoformat(),
            "expires_at": (
                utcnow()
                + timedelta(
                    days=request.get("retention_days") or get_settings().default_retention_days
                )
            ).isoformat(),
            "files": [
                {
                    "name": f.name,
                    "partition_key": f.partition_key,
                    "size_bytes": f.size_bytes,
                    "row_count": f.row_count,
                    "checksum": f.checksum,
                }
                for f in files
            ],
            "totals": {
                "file_count": len(files),
                "total_bytes": total_bytes,
                "total_rows": total_rows,
            },
            "lineage": {
                "sources": sources,
                "query_fingerprint": "sha256:"
                + hashlib.sha256(
                    f"{domain}|{request['period']}|{request.get('fund_scope')}".encode()
                ).hexdigest(),
                "worker_version": "extract-worker/2.4.1",
            },
        }
        return manifest, "sha256:" + combined, total_bytes, total_rows

    @staticmethod
    def _fund_bucket(n: int) -> str:
        if n <= 1:
            return "single"
        if n <= 10:
            return "small"
        if n <= 100:
            return "medium"
        return "large"

    # ---- event emission ----

    async def _emit_ready(
        self,
        extract_id: str,
        row: dict[str, Any],
        run_id: str,
        files: list[WrittenFile],
        duration: int,
        expires_at,
        schema_version: str,
    ) -> None:
        request = row["request"]
        domain = row["domain"]
        combined_checksum = (
            "sha256:"
            + hashlib.sha256("".join(sorted(f.checksum for f in files)).encode()).hexdigest()
        )
        files_endpoint = f"/api/v1/extracts/{extract_id}/files"
        evt = ExtractReadyEvent(
            event_id=f"evt_{utcnow().strftime('%Y%m%d_%H%M%S')}_{extract_id[-4:]}",
            emitted_at=utcnow(),
            source="extract-api",
            extract_id=extract_id,
            idempotency_key=row["idempotency_key"],
            scope=EventScope(
                domain=domain,
                period_start=request["period"]["start"],
                period_end=request["period"]["end"],
                as_of=request.get("as_of"),
                fund_scope=[f.partition_key for f in files],
                frequency=(Frequency(request["frequency"]) if request.get("frequency") else None),
            ),
            artifact=EventArtifact(
                file_count=len(files),
                total_bytes=sum(f.size_bytes for f in files),
                total_rows=sum(f.row_count for f in files),
                format=files[0].format,
                compression=files[0].compression,
                checksum=combined_checksum,
                schema_version=schema_version,
                files_endpoint=files_endpoint,
                expires_at=expires_at,
            ),
            lineage=EventLineage(
                run_id=run_id,
                sources=list(get_domain(domain).sources) if get_domain(domain) else [],
                produced_by="extract-worker",
                duration_seconds=duration,
            ),
            requester=EventRequester(
                app_id=row["app_id"],
                correlation_id=request.get("requester", {}).get("correlation_id"),
            ),
        )
        await self._route_notification(row, evt)
        get_metrics().inc(
            "extract.events.published",
            labels={"event_type": "extract.ready", "domain": domain},
        )

    async def _emit_failed(
        self,
        extract_id: str,
        row: dict[str, Any],
        code: ErrorCode,
        message: str,
        retries_attempted: int,
        partial_result: dict[str, Any] | None,
    ) -> None:
        request = row["request"]
        domain = row["domain"]
        evt = ExtractFailedEvent(
            event_id=f"evt_{utcnow().strftime('%Y%m%d_%H%M%S')}_{extract_id[-4:]}",
            emitted_at=utcnow(),
            source="extract-api",
            extract_id=extract_id,
            idempotency_key=row["idempotency_key"],
            scope=EventScope(
                domain=domain,
                period_start=request["period"]["start"],
                period_end=request["period"]["end"],
                as_of=request.get("as_of"),
                fund_scope=request.get("fund_scope") or [],
                frequency=(Frequency(request["frequency"]) if request.get("frequency") else None),
            ),
            error=ExtractFailedError(
                code=code,
                message=message,
                retries_attempted=retries_attempted,
                partial_result=partial_result,
            ),
            requester=EventRequester(
                app_id=row["app_id"],
                correlation_id=request.get("requester", {}).get("correlation_id"),
            ),
        )
        await self._route_notification(row, evt)
        get_metrics().inc(
            "extract.events.published",
            labels={"event_type": "extract.failed", "domain": domain},
        )

    async def _route_notification(
        self,
        row: dict[str, Any],
        event,
    ) -> None:
        request = row["request"]
        notification = request.get("notification") or {"mode": "event"}
        mode = notification.get("mode", "event")
        payload = event.model_dump(mode="json")
        if mode == "event":
            topic = notification.get("topic") or f"fund-services.{row['domain']}.extract.ready"
            if event.event_type == "extract.failed":
                topic = topic.replace(".ready", ".failed")
            await self.event_bus.publish(topic, event, partition_key=row["extract_id"])
        elif mode == "webhook":
            await self.webhook_delivery.schedule(
                row["app_id"],
                notification["callback_url"],
                event.event_type,
                payload,
            )
        # mode == none → only polling
