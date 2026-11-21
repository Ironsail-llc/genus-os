"""The bridge's real engine client reaches the engine's period route.

``POST /api/rooms/{id}/periods`` on the bridge checks access and then proxies to
the engine through ``routers._engine_client.engine_request`` with its own
service credential and ``requested_by``. This drives that client, unmocked,
against the engine app in this process with engine authentication on, so the
token the bridge really mints meets the scope check the engine really applies.
Only the transport (in-process instead of a socket), the account lookup and the
period request itself are stand-ins.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter

ROOM = "7a0c0e8e-63c4-4c5b-9a43-0d6f6f0c2b11"
ALICE = "5b8e4a8a-0f6e-4e43-8f53-1f9d1d3c2a77"
TENANT = "test-tenant"


@pytest.fixture
def engine_app(monkeypatch):
    from robothor.auth import tokens
    from robothor.engine.health import create_health_app

    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "bridge-to-engine-test-key-at-least-32-bytes")
    tokens.reset_signing_key_cache()
    config = MagicMock()
    config.tenant_id = TENANT
    config.bot_token = ""
    config.port = 18800
    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(config, runner=None)
    yield app
    tokens.reset_signing_key_cache()


@pytest.fixture
def requests(monkeypatch, engine_app):
    """Route the client's calls into ``engine_app``; record what the engine was asked."""
    from routers import _engine_client

    from robothor.rooms.models import Edition

    calls: list[Any] = []

    async def request_period(actor, room_id, grain, anchor):
        calls.append((actor, room_id, grain, anchor))
        edition = Edition(
            tenant_id=TENANT,
            id="0d1c7a3e-8a4b-4f0e-9c55-6a3d7c1e2f90",
            room_id=room_id,
            seq=1,
            kind="edition",
            status="building",
            preview=False,
            period_start=date(2026, 11, 20),
            period_end=date(2026, 11, 20),
            grain=grain,
            revision=1,
            created_at=datetime(2026, 11, 21, 9, 0, tzinfo=UTC),
        )
        return edition, True

    monkeypatch.setattr("robothor.rooms.periods.request_period", request_period)
    account = {"id": ALICE, "tenant_id": TENANT, "role": "member", "status": "active"}
    monkeypatch.setattr(
        "robothor.auth.accounts.get_account_by_id", lambda uid: account if uid == ALICE else None
    )

    real_client = httpx.AsyncClient

    def in_process(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.ASGITransport(app=engine_app), **kwargs)

    monkeypatch.setattr(_engine_client.httpx, "AsyncClient", in_process)
    return calls


async def test_the_bridges_proxy_call_is_accepted_and_acts_for_the_requester(requests):
    from routers._engine_client import engine_request

    status, body = await engine_request(
        "POST",
        f"/api/rooms/{ROOM}/periods",
        json={"grain": "day", "anchor": "2026-11-20", "requested_by": ALICE},
        tenant=TENANT,
    )

    assert status == 202, body
    assert body["status"] == "building"
    actor = requests[0][0]
    assert (actor.user_id, actor.role, actor.tenant_id) == (ALICE, "member", TENANT)


async def test_a_requester_without_an_account_here_is_not_served(requests):
    from routers._engine_client import engine_request

    status, _ = await engine_request(
        "POST",
        f"/api/rooms/{ROOM}/periods",
        json={"grain": "day", "requested_by": "9c3f1d2e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"},
        tenant=TENANT,
    )

    assert status == 404
    assert requests == []
