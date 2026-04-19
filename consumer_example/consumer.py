"""Example downstream consumer (§11.3 Consumer Responsibilities).

Demonstrates:
- Subscribing to an event topic via the in-process event bus.
- Dedup by event_id.
- Fetching the files endpoint through the API (entitlement check).
- Verifying per-file sha256 checksums from the manifest before processing.
- Committing "offsets" (handler returns successfully → bus advances).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

log = logging.getLogger("consumer")


class ExampleConsumer:
    def __init__(
        self,
        *,
        api_base_url: str,
        token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.token = token
        self.http = http_client or httpx.AsyncClient()
        self.seen_event_ids: set[str] = set()
        self.processed_extracts: list[dict[str, Any]] = []

    async def handle(self, event: dict[str, Any]) -> None:
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        if event_id in self.seen_event_ids:
            log.info("duplicate event skipped", extra={"event_id": event_id})
            return
        if event_type != "extract.ready":
            log.info("ignoring event_type", extra={"event_type": event_type})
            self.seen_event_ids.add(event_id or "")
            return

        extract_id = event["extract_id"]
        files_endpoint = event["artifact"]["files_endpoint"]
        log.info(
            "processing extract.ready",
            extra={"extract_id": extract_id, "event_id": event_id},
        )
        files_resp = await self.http.get(
            f"{self.api_base_url}{files_endpoint}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        files_resp.raise_for_status()
        body = files_resp.json()

        downloaded: list[dict[str, Any]] = []
        for f in body["files"]:
            r = await self.http.get(f["download_url"])
            r.raise_for_status()
            actual = "sha256:" + hashlib.sha256(r.content).hexdigest()
            if actual != f["checksum"]:
                raise RuntimeError(
                    f"checksum mismatch for {f['partition_key']}: "
                    f"expected {f['checksum']} got {actual}"
                )
            downloaded.append(
                {
                    "partition_key": f["partition_key"],
                    "size_bytes": len(r.content),
                }
            )

        self.seen_event_ids.add(event_id or "")
        self.processed_extracts.append(
            {"extract_id": extract_id, "files": downloaded}
        )
        log.info(
            "extract processed",
            extra={"extract_id": extract_id, "file_count": len(downloaded)},
        )
