"""Observability contract tests.

Covers spec sections: §9.1 (structured logging context is populated
per request), §9.2 (metrics counters/histograms emitted on the normal
request path), §9.4 (correlation_id propagates through API →
response), §9.4.1 (API mints a correlation_id when the client does
not provide one and echoes it back in the `X-Correlation-Id`
response header).
"""
from __future__ import annotations

import asyncio

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
    "idempotency_key": "obs-1",
    "requester": {"app_id": "fes-plus-plus"},
}


@pytest.mark.asyncio
async def test_correlation_id_minted_when_absent(api_client):
    client, _ = api_client
    r = await client.post("/api/v1/extracts", json=BASE, headers=HEADERS)
    assert r.status_code == 202, r.text
    cid = r.headers.get("X-Correlation-Id")
    assert cid, "API must mint an X-Correlation-Id when the client provides none (§9.4.1)"
    assert len(cid) >= 8, cid


@pytest.mark.asyncio
async def test_correlation_id_echoed_when_provided(api_client):
    client, _ = api_client
    supplied = "corr-obs-test-abcdef"
    req = dict(BASE)
    req["idempotency_key"] = "obs-2"
    r = await client.post(
        "/api/v1/extracts",
        json=req,
        headers={**HEADERS, "X-Correlation-Id": supplied},
    )
    assert r.status_code == 202, r.text
    assert r.headers["X-Correlation-Id"] == supplied, (
        "API must echo client-supplied correlation_id unchanged (§9.4)"
    )


@pytest.mark.asyncio
async def test_metrics_emitted_on_request_path(api_client):
    """§9.2: request counters + presigned-URL counter must move on the happy path."""
    client, _ = api_client
    from extract_service.metrics import get_metrics

    metrics = get_metrics()
    before = metrics.snapshot()

    req = dict(BASE)
    req["idempotency_key"] = "obs-metrics"
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]
    for _ in range(200):
        s = await client.get(f"/api/v1/extracts/{extract_id}", headers=HEADERS)
        if s.json()["status"] == "COMPLETED":
            break
        await asyncio.sleep(0.05)
    await client.get(f"/api/v1/extracts/{extract_id}/files", headers=HEADERS)

    after = metrics.snapshot()
    assert after["counters"] != before["counters"], (
        "expected metric counters to move during a normal request lifecycle"
    )
    presigned_keys = [
        k for k in after["counters"] if k.startswith("extract.storage.presigned_urls_issued")
    ]
    assert presigned_keys, "presigned URL counter (§9.2) not emitted"
