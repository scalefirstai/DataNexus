"""End-to-end smoke test for the extract service.

Usage:
    python -m scripts.smoke

Starts the FastAPI app in-process, submits an extract request, subscribes to
the event bus, waits for extract.ready, fetches files, verifies checksums.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consumer_example.consumer import ExampleConsumer
from extract_service import main as api_module
from extract_service.event_bus import get_event_bus


TOKEN = "demo-token-fes"
APP_ID = "fes-plus-plus"


async def run() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_module.app),
        base_url="http://testserver",
    ) as client:
        # Trigger lifespan so worker starts
        async with api_module.app.router.lifespan_context(api_module.app):
            event_bus = get_event_bus()
            consumer = ExampleConsumer(
                api_base_url="http://testserver",
                token=TOKEN,
                http_client=client,
            )
            done = asyncio.Event()

            async def handler(evt: dict) -> None:
                await consumer.handle(evt)
                done.set()

            event_bus.subscribe(
                "fund-services.nav-ledger.extract.ready",
                "calc-engine.nav-ledger.extract",
                handler,
            )

            req = {
                "domain": "nav-ledger",
                "period": {"start": "2025-01-01", "end": "2025-12-31"},
                "as_of": "2026-01-02T23:59:59Z",
                "fund_scope": ["fund_A", "fund_B", "fund_C"],
                "frequency": "yearly",
                "output": {"format": "ndjson", "compression": "gzip"},
                "notification": {
                    "mode": "event",
                    "topic": "fund-services.nav-ledger.extract.ready",
                },
                "idempotency_key": "smoke-test-001",
                "priority": "normal",
                "requester": {"app_id": APP_ID, "correlation_id": "corr-smoke"},
            }
            r = await client.post(
                "/api/v1/extracts",
                json=req,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            r.raise_for_status()
            extract_id = r.json()["extract_id"]
            print(f"submitted {extract_id}")

            try:
                await asyncio.wait_for(done.wait(), timeout=15)
            except asyncio.TimeoutError:
                status_resp = await client.get(
                    f"/api/v1/extracts/{extract_id}",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                print("TIMEOUT — final status:")
                print(json.dumps(status_resp.json(), indent=2, default=str))
                raise

            print("consumer processed:")
            print(json.dumps(consumer.processed_extracts, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(run())
