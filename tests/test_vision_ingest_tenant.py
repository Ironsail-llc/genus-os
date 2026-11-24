"""The vision service names its tenant when it posts an event to the orchestrator.

It is one process per instance, bound to the tenant its environment names
(``ROBOTHOR_TENANT_ID``, then ``ROBOTHOR_DEFAULT_TENANT``). It used to post with
no tenant, so the orchestrator stored every camera event in the default tenant.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.vision.service import VisionService


@pytest.fixture
def service(tmp_path):
    dirs = [tmp_path / d for d in ("snapshots", "faces", "state")]
    for d in dirs:
        d.mkdir()
    return VisionService(
        orchestrator_url="http://orchestrator.invalid",
        snapshot_dir=dirs[0],
        face_data_dir=dirs[1],
        state_dir=dirs[2],
    )


def _capture_post():
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=200))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_an_event_is_posted_with_the_process_tenant(service, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "tenant-a")
    client = _capture_post()
    with patch("httpx.AsyncClient", return_value=client):
        await service.ingest_event("Alice arrived", {"camera": "front"})
    assert client.post.await_args.kwargs["headers"]["X-Tenant-Id"] == "tenant-a"


async def test_an_unbound_process_sends_no_tenant_and_lets_the_orchestrator_decide(
    service, monkeypatch
):
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
    monkeypatch.delenv("ROBOTHOR_DEFAULT_TENANT", raising=False)
    client = _capture_post()
    with patch("httpx.AsyncClient", return_value=client):
        await service.ingest_event("Alice arrived", {"camera": "front"})
    assert "X-Tenant-Id" not in (client.post.await_args.kwargs.get("headers") or {})
