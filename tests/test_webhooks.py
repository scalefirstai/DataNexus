"""Webhook delivery with HMAC signature verification."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest


HEADERS = {"Authorization": "Bearer demo-token-fes"}


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.received: list[httpx.Request] = []
        self.status_code = 200

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        return httpx.Response(self.status_code, json={"ok": True}, request=request)


@pytest.mark.asyncio
async def test_webhook_delivered_with_valid_hmac(api_client):
    client, api_module = api_client
    mock = _MockTransport()
    api_module.app.state.webhook_delivery.set_transport(mock)

    req = {
        "domain": "nav-ledger",
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "fund_scope": ["fund_A"],
        "output": {"format": "ndjson"},
        "notification": {
            "mode": "webhook",
            "callback_url": "https://hooks.test/extract",
        },
        "idempotency_key": "webhook-test-1",
        "requester": {"app_id": "fes-plus-plus"},
    }
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]

    for _ in range(200):
        status = await client.get(
            f"/api/v1/extracts/{extract_id}", headers=HEADERS
        )
        if status.json()["status"] in ("COMPLETED", "FAILED", "PARTIAL"):
            break
        await asyncio.sleep(0.05)

    for _ in range(40):
        if mock.received:
            break
        await asyncio.sleep(0.05)

    assert mock.received, "webhook never received"
    req_out = mock.received[0]
    body = req_out.content
    sig = req_out.headers["x-extract-signature"]
    expected = (
        "sha256="
        + hmac.new(
            b"fes-webhook-secret", body, hashlib.sha256
        ).hexdigest()
    )
    assert hmac.compare_digest(sig, expected)
    assert req_out.headers["x-extract-event"] == "extract.ready"
    assert "x-extract-delivery" in req_out.headers
    payload = json.loads(body)
    assert payload["event_type"] == "extract.ready"
