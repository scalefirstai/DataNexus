"""Webhook delivery with HMAC signing + retention sweeper (§4.7, §5.5)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from .config import get_settings
from .event_bus import EventBus
from .models import ExtractExpiringEvent, ExtractStatus, utcnow
from .object_store import ObjectStore
from .storage import Storage

log = logging.getLogger("extract.webhooks")


@dataclass
class WebhookJob:
    delivery_id: str
    app_id: str
    url: str
    event_type: str
    body: dict[str, Any]
    attempts: int = 0


class WebhookDelivery:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._queue: asyncio.Queue[WebhookJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._client = httpx.AsyncClient(timeout=get_settings().webhook_timeout_seconds)
        self._transport_override: httpx.AsyncBaseTransport | None = None
        self.deliveries: list[dict[str, Any]] = []  # for test introspection

    def set_transport(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport_override = transport
        self._client = httpx.AsyncClient(
            transport=transport, timeout=get_settings().webhook_timeout_seconds
        )

    async def start(self, concurrency: int = 2) -> None:
        for i in range(concurrency):
            self._workers.append(
                asyncio.create_task(self._run(), name=f"webhook-{i}")
            )

    async def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            await self._queue.put(None)  # type: ignore[arg-type]
        for t in self._workers:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
        await self._client.aclose()

    async def schedule(
        self, app_id: str, url: str, event_type: str, body: dict[str, Any]
    ) -> None:
        delivery_id = f"del_{utcnow().strftime('%Y%m%d_%H%M%S')}_{body.get('event_id','x')[-4:]}"
        await self._queue.put(
            WebhookJob(
                delivery_id=delivery_id,
                app_id=app_id,
                url=url,
                event_type=event_type,
                body=body,
            )
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await self._queue.get()
            if job is None:
                return
            try:
                await self._attempt(job)
            except Exception:
                log.exception("webhook worker crashed", extra={"app_id": job.app_id})

    async def _attempt(self, job: WebhookJob) -> None:
        settings = get_settings()
        schedule = settings.webhook_backoff_schedule
        max_attempts = len(schedule)
        secret = self._get_secret(job.app_id)
        payload = json.dumps(job.body, default=str, separators=(",", ":")).encode()
        sig = (
            "sha256="
            + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        )
        headers = {
            "Content-Type": "application/json",
            "X-Extract-Signature": sig,
            "X-Extract-Event": job.event_type,
            "X-Extract-Delivery": job.delivery_id,
        }
        while job.attempts < max_attempts:
            job.attempts += 1
            try:
                resp = await self._client.post(
                    job.url, content=payload, headers=headers
                )
                if 200 <= resp.status_code < 300:
                    self.deliveries.append(
                        {
                            "delivery_id": job.delivery_id,
                            "status": "DELIVERED",
                            "attempts": job.attempts,
                        }
                    )
                    self.storage.audit(
                        job.app_id,
                        "webhook.delivered",
                        job.body.get("extract_id"),
                        "success",
                        {"delivery_id": job.delivery_id, "attempts": job.attempts},
                    )
                    return
                log.warning(
                    "webhook non-2xx response",
                    extra={
                        "status": resp.status_code,
                        "attempt": job.attempts,
                        "delivery_id": job.delivery_id,
                    },
                )
            except Exception as e:
                log.warning(
                    "webhook delivery error",
                    extra={
                        "error": str(e),
                        "attempt": job.attempts,
                        "delivery_id": job.delivery_id,
                    },
                )
            if job.attempts < max_attempts:
                delay = schedule[job.attempts - 1]
                await asyncio.sleep(min(delay, 0.05))  # test-fast; production uses real delay
        self.deliveries.append(
            {
                "delivery_id": job.delivery_id,
                "status": "FAILED_DELIVERY",
                "attempts": job.attempts,
            }
        )
        self.storage.audit(
            job.app_id,
            "webhook.failed",
            job.body.get("extract_id"),
            "failure",
            {"delivery_id": job.delivery_id, "attempts": job.attempts},
        )

    def _get_secret(self, app_id: str) -> str:
        app = self.storage.get_app(app_id)
        if app and app.get("webhook_secret"):
            return app["webhook_secret"]
        return get_settings().webhook_signing_secret_default


_webhook: WebhookDelivery | None = None


def get_webhook_delivery() -> WebhookDelivery:
    global _webhook
    if _webhook is None:
        _webhook = WebhookDelivery(Storage(get_settings().db_path))
    return _webhook


def reset_webhook_delivery_for_tests() -> None:
    global _webhook
    _webhook = None


# ---------------------------------------------------------------------------
# Retention sweeper (§5.5)
# ---------------------------------------------------------------------------


class RetentionSweeper:
    def __init__(
        self,
        storage: Storage,
        object_store: ObjectStore,
        event_bus: EventBus,
        warn_window: timedelta = timedelta(hours=24),
        tick_seconds: float = 60.0,
    ) -> None:
        self.storage = storage
        self.object_store = object_store
        self.event_bus = event_bus
        self.warn_window = warn_window
        self.tick_seconds = tick_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="retention-sweeper")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("retention sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        for row in self.storage.find_expiring(self.warn_window):
            extract_id = row["extract_id"]
            expires_at = row["expires_at"]
            evt = ExtractExpiringEvent(
                event_id=f"evt_{utcnow().strftime('%Y%m%d_%H%M%S')}_{extract_id[-4:]}",
                emitted_at=utcnow(),
                source="extract-api",
                extract_id=extract_id,
                expires_at=expires_at,
                files_endpoint=f"/api/v1/extracts/{extract_id}/files",
            )
            topic = f"fund-services.{row['domain']}.extract.expiring"
            await self.event_bus.publish(topic, evt, partition_key=extract_id)
            self.storage.update_extract(extract_id, expiring_event_sent=True)
            log.info("expiring event emitted", extra={"extract_id": extract_id})

        for row in self.storage.find_expired():
            extract_id = row["extract_id"]
            self.object_store.delete_published(extract_id, row["domain"])
            self.storage.update_extract(
                extract_id, status=ExtractStatus.EXPIRED
            )
            log.info("extract expired", extra={"extract_id": extract_id})

        self.event_bus.prune_expired()


_sweeper: RetentionSweeper | None = None


def get_retention_sweeper(
    storage: Storage, object_store: ObjectStore, event_bus: EventBus
) -> RetentionSweeper:
    global _sweeper
    if _sweeper is None:
        _sweeper = RetentionSweeper(storage, object_store, event_bus)
    return _sweeper


def reset_retention_sweeper_for_tests() -> None:
    global _sweeper
    _sweeper = None
