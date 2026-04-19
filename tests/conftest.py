"""Shared pytest fixtures."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extract_service import (  # noqa: E402
    config,
    event_bus,
    metrics,
    object_store,
    storage,
    webhooks,
)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACT_BASE_DIR", str(tmp_path))
    config._settings = None  # type: ignore[attr-defined]
    storage._storage = None  # type: ignore[attr-defined]
    object_store._store = None  # type: ignore[attr-defined]
    event_bus._bus = None  # type: ignore[attr-defined]
    webhooks._webhook = None  # type: ignore[attr-defined]
    webhooks._sweeper = None  # type: ignore[attr-defined]
    metrics._metrics = None  # type: ignore[attr-defined]
    yield tmp_path


@pytest.fixture
async def api_client(isolated_env):
    from extract_service import main as api_module  # fresh import per test

    async with api_module.app.router.lifespan_context(api_module.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url="http://testserver",
        ) as client:
            yield client, api_module
