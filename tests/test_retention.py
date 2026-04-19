"""Retention sweeper: extract.expiring event + expiry cleanup.

Covers spec sections: §3.2.3 (files endpoint 410 Gone for EXPIRED),
§4.1 (topic naming for expiring), §4.5 (extract.expiring event schema),
§5.5 (retention policy), §5.5.1 (retention/in-flight-download invariant),
§12.5 (consumer falls behind — API as recovery path).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest


HEADERS = {"Authorization": "Bearer demo-token-fes"}
BASE = {
    "domain": "nav-ledger",
    "period": {"start": "2025-01-01", "end": "2025-12-31"},
    "fund_scope": ["fund_A"],
    "output": {"format": "ndjson"},
    "notification": {
        "mode": "event",
        "topic": "fund-services.nav-ledger.extract.ready",
    },
    "idempotency_key": "retention-1",
    "requester": {"app_id": "fes-plus-plus"},
}


@pytest.mark.asyncio
async def test_expiring_event_and_cleanup(api_client):
    from extract_service.event_bus import get_event_bus
    from extract_service.models import utcnow
    from extract_service.storage import get_storage

    client, api_module = api_client
    bus = get_event_bus()
    storage = get_storage()

    received: list[dict] = []

    async def handler(evt):
        received.append(evt)

    bus.subscribe(
        "fund-services.nav-ledger.extract.expiring",
        "test.calc.expiring",
        handler,
    )

    r = await client.post("/api/v1/extracts", json=BASE, headers=HEADERS)
    extract_id = r.json()["extract_id"]

    for _ in range(200):
        s = await client.get(f"/api/v1/extracts/{extract_id}", headers=HEADERS)
        if s.json()["status"] == "COMPLETED":
            break
        await asyncio.sleep(0.05)

    storage.update_extract(
        extract_id, expires_at=utcnow() + timedelta(hours=1)
    )

    sweeper = api_module.app.state.sweeper
    await sweeper.run_once()
    await asyncio.sleep(0.2)
    expiring = [e for e in received if e["event_type"] == "extract.expiring"]
    assert expiring, received

    # Spec §4.5 (v1.1): extract.expiring carries scope + requester +
    # idempotency_key for symmetry with extract.ready / extract.failed.
    evt = expiring[0]
    assert evt["event_version"] == "1.1"
    assert evt["idempotency_key"] == "retention-1"
    assert evt["scope"]["domain"] == "nav-ledger"
    assert evt["scope"]["fund_scope"] == ["fund_A"]
    assert evt["scope"]["period_start"] == "2025-01-01"
    assert evt["requester"]["app_id"] == "fes-plus-plus"

    storage.update_extract(
        extract_id, expires_at=utcnow() - timedelta(seconds=1)
    )
    await sweeper.run_once()

    s = await client.get(f"/api/v1/extracts/{extract_id}", headers=HEADERS)
    assert s.json()["status"] == "EXPIRED"

    # Spec §3.2.3 (v1.1): files endpoint returns 410 Gone for EXPIRED
    # extracts so replays of old events get a terminal signal, not 404.
    gone = await client.get(
        f"/api/v1/extracts/{extract_id}/files", headers=HEADERS
    )
    assert gone.status_code == 410, gone.text
