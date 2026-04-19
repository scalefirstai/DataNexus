"""In-memory event bus implementing §4 of the spec.

- Topic-based pub/sub with per-consumer-group offsets
- At-least-once delivery (consumers must be idempotent)
- Per-partition ordering keyed by extract_id
- 5-attempt delivery retries with DLQ routing ({topic}.dlq)
- 7-day event retention
- Schema versioning via `event_version` on the event payload
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .config import get_settings
from .models import BaseEvent, utcnow

log = logging.getLogger("extract.event_bus")

Handler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class StoredEvent:
    offset: int
    topic: str
    partition_key: str
    event: dict[str, Any]
    emitted_ts: float


@dataclass
class Subscription:
    group_id: str
    handler: Handler
    offset: int = 0
    task: asyncio.Task | None = None
    paused: bool = False
    retries_by_event: dict[str, int] = field(default_factory=dict)


class EventBus:
    MAX_DELIVERY_ATTEMPTS = 5

    def __init__(self) -> None:
        self._topics: dict[str, list[StoredEvent]] = defaultdict(list)
        self._subs: dict[str, list[Subscription]] = defaultdict(list)
        self._cond = asyncio.Condition()
        self._next_offset: dict[str, int] = defaultdict(int)
        self._dlq: dict[str, list[StoredEvent]] = defaultdict(list)
        self._bus_available = True

    # --- admin ---

    def pause(self) -> None:
        self._bus_available = False

    def resume(self) -> None:
        self._bus_available = True

    def topic_depth(self, topic: str) -> int:
        return len(self._topics.get(topic, []))

    def dlq_depth(self, topic: str) -> int:
        return len(self._dlq.get(f"{topic}.dlq", []))

    def get_events(self, topic: str) -> list[dict[str, Any]]:
        return [e.event for e in self._topics.get(topic, [])]

    def get_dlq(self, topic: str) -> list[dict[str, Any]]:
        return [e.event for e in self._dlq.get(f"{topic}.dlq", [])]

    # --- publish ---

    async def publish(
        self,
        topic: str,
        event: BaseEvent | dict[str, Any],
        partition_key: str,
    ) -> None:
        payload = event.model_dump(mode="json") if isinstance(event, BaseEvent) else event
        async with self._cond:
            while not self._bus_available:
                await self._cond.wait()
            offset = self._next_offset[topic]
            self._next_offset[topic] = offset + 1
            stored = StoredEvent(
                offset=offset,
                topic=topic,
                partition_key=partition_key,
                event=payload,
                emitted_ts=utcnow().timestamp(),
            )
            self._topics[topic].append(stored)
            self._cond.notify_all()
            log.info(
                "published event",
                extra={
                    "topic": topic,
                    "partition_key": partition_key,
                    "event_id": payload.get("event_id"),
                    "event_type": payload.get("event_type"),
                    "offset": offset,
                },
            )

    # --- subscribe ---

    def subscribe(self, topic: str, group_id: str, handler: Handler) -> Subscription:
        sub = Subscription(group_id=group_id, handler=handler)
        self._subs[topic].append(sub)
        sub.task = asyncio.create_task(
            self._run_subscription(topic, sub), name=f"sub:{topic}:{group_id}"
        )
        return sub

    async def unsubscribe(self, topic: str, sub: Subscription) -> None:
        if sub.task:
            sub.task.cancel()
            try:
                await sub.task
            except asyncio.CancelledError:
                pass
        if sub in self._subs.get(topic, []):
            self._subs[topic].remove(sub)

    async def _run_subscription(self, topic: str, sub: Subscription) -> None:
        try:
            while True:
                async with self._cond:
                    while sub.offset >= len(self._topics[topic]) or sub.paused:
                        await self._cond.wait()
                    event = self._topics[topic][sub.offset]
                await self._deliver(topic, sub, event)
                sub.offset += 1
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception(
                "subscription crashed",
                extra={"topic": topic, "group_id": sub.group_id},
            )

    async def _deliver(self, topic: str, sub: Subscription, event: StoredEvent) -> None:
        event_id = event.event.get("event_id", "unknown")
        attempt = 0
        while attempt < self.MAX_DELIVERY_ATTEMPTS:
            attempt += 1
            try:
                await sub.handler(event.event)
                return
            except Exception:
                log.warning(
                    "handler raised, retrying",
                    extra={
                        "topic": topic,
                        "group_id": sub.group_id,
                        "event_id": event_id,
                        "attempt": attempt,
                    },
                )
                await asyncio.sleep(min(0.1 * 2**attempt, 2.0))
        log.error(
            "routing event to DLQ after max retries",
            extra={
                "topic": topic,
                "group_id": sub.group_id,
                "event_id": event_id,
            },
        )
        self._dlq[f"{topic}.dlq"].append(event)

    # --- retention ---

    def prune_expired(self) -> None:
        retention = timedelta(days=get_settings().event_retention_days)
        cutoff = (utcnow() - retention).timestamp()
        for topic, events in list(self._topics.items()):
            kept = [e for e in events if e.emitted_ts >= cutoff]
            self._topics[topic] = kept


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_event_bus_for_tests() -> None:
    global _bus
    _bus = None
