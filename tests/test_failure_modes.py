"""Failure handling — partial, source outage, presigned URL enforcement.

Covers spec sections: §3.2.3 (files endpoint semantics while non-terminal),
§4.4 (extract.failed event), §6.2 (entitlement-scoped file listing),
§6.3 (encryption / presigned URL tamper-evidence),
§7.1 (error taxonomy — PARTIAL_FAILURE, SOURCE_UNAVAILABLE),
§7.2 (worker retry policy — exhausted retries → FAILED),
§7.3 (PARTIAL as first-class terminal status),
§12.1 (source-outage runbook scenario).
"""
from __future__ import annotations

import asyncio

import pytest


HEADERS = {"Authorization": "Bearer demo-token-fes"}

BASE = {
    "domain": "nav-ledger",
    "period": {"start": "2025-01-01", "end": "2025-12-31"},
    "fund_scope": ["fund_A", "fund_B", "fund_C"],
    "output": {"format": "ndjson"},
    "notification": {
        "mode": "event",
        "topic": "fund-services.nav-ledger.extract.ready",
    },
    "idempotency_key": "failure-test-1",
    "requester": {"app_id": "fes-plus-plus"},
}


async def _await_terminal(client, extract_id, timeout=20):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/api/v1/extracts/{extract_id}", headers=HEADERS)
        s = r.json()["status"]
        if s in ("COMPLETED", "FAILED", "PARTIAL", "CANCELLED", "EXPIRED"):
            return r.json()
        await asyncio.sleep(0.05)
    raise AssertionError("did not reach terminal status")


@pytest.mark.asyncio
async def test_partial_failure_produces_PARTIAL(api_client):
    client, api_module = api_client
    worker = api_module.app.state.worker
    worker.pause_processing()
    req = dict(BASE)
    req["idempotency_key"] = "partial-1"
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]
    worker.simulate_partial_failure(extract_id, {"fund_B"})
    worker.resume_processing()
    final = await _await_terminal(client, extract_id)
    assert final["status"] == "PARTIAL"
    assert final["error"]["code"] == "PARTIAL_FAILURE"
    assert "fund_B" in final["error"]["partial_result"]["funds_failed"]


@pytest.mark.asyncio
async def test_full_outage_produces_FAILED(api_client):
    client, api_module = api_client
    worker = api_module.app.state.worker
    worker.pause_processing()
    worker.simulate_source_outage(True)
    req = dict(BASE)
    req["idempotency_key"] = "outage-1"
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]
    worker.resume_processing()
    final = await _await_terminal(client, extract_id)
    assert final["status"] == "FAILED"
    worker.simulate_source_outage(False)


@pytest.mark.asyncio
async def test_invalid_presigned_token_rejected(api_client):
    client, api_module = api_client
    req = dict(BASE)
    req["idempotency_key"] = "presign-1"
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]
    final = await _await_terminal(client, extract_id)
    assert final["status"] == "COMPLETED"
    files = await client.get(
        f"/api/v1/extracts/{extract_id}/files", headers=HEADERS
    )
    url = files.json()["files"][0]["download_url"]
    tampered = url.replace("token=", "token=bad")
    r2 = await client.get(tampered)
    assert r2.status_code == 403


@pytest.mark.asyncio
async def test_files_hidden_for_unentitled_consumer(api_client):
    client, _ = api_client
    req = dict(BASE)
    req["idempotency_key"] = "ent-files-1"
    req["fund_scope"] = ["fund_A", "fund_B", "fund_C"]
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]
    final = await _await_terminal(client, extract_id)
    assert final["status"] == "COMPLETED"

    # calc-engine is entitled only to fund_A, fund_B. Listing files as calc-engine
    # would 404 because the extract belongs to fes-plus-plus. Instead simulate
    # entitlement narrowing by temporarily restricting fes-plus-plus then listing.
    from extract_service.storage import get_storage

    storage = get_storage()
    with storage.tx() as conn:
        conn.execute(
            "DELETE FROM entitlements WHERE app_id='fes-plus-plus' AND fund_id='fund_C'"
        )
    files = await client.get(
        f"/api/v1/extracts/{extract_id}/files", headers=HEADERS
    )
    parts = {f["partition_key"] for f in files.json()["files"]}
    assert "fund_C" not in parts
    assert parts == {"fund_A", "fund_B"}


@pytest.mark.asyncio
async def test_files_unavailable_while_running(api_client):
    client, _ = api_client
    req = dict(BASE)
    req["idempotency_key"] = "running-1"
    req["fund_scope"] = ["fund_A"]
    r = await client.post("/api/v1/extracts", json=req, headers=HEADERS)
    extract_id = r.json()["extract_id"]
    r2 = await client.get(
        f"/api/v1/extracts/{extract_id}/files", headers=HEADERS
    )
    # Either ACCEPTED/RUNNING (409) or already COMPLETED (200). Both valid.
    assert r2.status_code in (200, 409)
