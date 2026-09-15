"""The Channels page's two reads: what exists, and does it work.

The engine owns both answers — a channel is an object in that process holding
that process's credentials — so these routes are a gate, a composition and a
proxy, and the tests are about exactly those three things:

* the operator gate, on the READ as well as the write: the listing enumerates
  which channels this appliance has configured, and a verify sends a real
  message;
* the composition: the engine knows nothing about pairing, so the access mode
  and the pending-code count are added here from the rows the bridge owns —
  and a database that is down makes that field ``null``, never ``0``, because
  "nobody is waiting" is a different claim from "I could not look";
* the proxy: a verify's verdict is the engine's, passed through with its status
  code, and never manufactured here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE_CHANNELS = {
    "channels": [
        {
            "name": "slack",
            "builtin": True,
            "configured": True,
            "health": {"channel": "slack", "configured": True, "ok": True, "team": "Acme"},
            "verify_available": True,
        },
        {
            "name": "event_bus",
            "builtin": True,
            "configured": False,
            "health": {"channel": "event_bus", "enabled": False},
            "verify_available": False,
        },
    ]
}

ENGINE_VERIFY = {
    "channel": "slack",
    "configured": True,
    "verify_available": True,
    "steps": [
        {"step": "auth", "ok": True, "detail": "team Acme"},
        {"step": "post", "ok": False, "detail": "not_in_channel"},
    ],
}


class FakeEngine:
    """Stands in for the engine's ``/api/admin/channels`` surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.status = 200

    async def __call__(self, method, path, *, json=None, timeout=30):
        self.calls.append((method, path, json))
        if path.endswith("/verify"):
            return self.status, ENGINE_VERIFY
        return self.status, ENGINE_CHANNELS


@pytest.fixture
def fake_engine():
    engine = FakeEngine()
    with patch("routers.channel_access.engine_request", new=engine):
        yield engine


@pytest.fixture
def local_state(monkeypatch):
    """The two facts the bridge adds: the mode, and who is waiting."""
    from routers import channel_access

    monkeypatch.setattr(channel_access.access, "access_mode", lambda name: "pairing")
    monkeypatch.setattr(channel_access.identities, "list_pending", lambda *a, **kw: [{}, {}])
    return channel_access


# ── The gate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("client_name", ["controls_client_as_viewer", "controls_client_as_service"])
def test_a_non_operator_cannot_list_channels(request, client_name, fake_engine, local_state):
    client = request.getfixturevalue(client_name)
    assert client.get("/api/channels").status_code == 403
    assert not fake_engine.calls, "a rejected caller must not reach the engine"


@pytest.mark.parametrize("client_name", ["controls_client_as_viewer", "controls_client_as_service"])
def test_a_non_operator_cannot_verify(request, client_name, fake_engine, local_state):
    client = request.getfixturevalue(client_name)
    response = client.post("/api/channels/slack/verify", json={})
    assert response.status_code == 403
    assert not fake_engine.calls


def test_another_tenants_operator_cannot_list_channels(
    controls_client_as_other_tenant_owner, fake_engine, local_state
):
    """One engine owns one set of channels. A second tenant's operator reading
    them would be reading somebody else's instance."""
    assert controls_client_as_other_tenant_owner.get("/api/channels").status_code == 403


# ── The listing ─────────────────────────────────────────────────────────


def test_it_composes_the_engine_listing_with_what_the_bridge_knows(
    controls_client_as_operator, fake_engine, local_state
):
    body = controls_client_as_operator.get("/api/channels").json()

    by_name = {entry["name"]: entry for entry in body["channels"]}
    assert by_name["slack"]["configured"] is True
    assert by_name["slack"]["health"]["team"] == "Acme"
    assert by_name["slack"]["access_mode"] == "pairing"
    assert by_name["slack"]["pending_pairings"] == 2
    assert ("GET", "/api/admin/channels", None) in fake_engine.calls


def test_a_database_that_is_down_makes_the_count_null_not_zero(
    controls_client_as_operator, fake_engine, local_state, monkeypatch
):
    """``0 pending`` is a claim that nobody is waiting to be let in. If the
    pairing rows could not be read, the honest answer is that we do not know —
    the same distinction the listing's ``configured: null`` draws."""

    def _down(*args, **kwargs):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(local_state.identities, "list_pending", _down)

    body = controls_client_as_operator.get("/api/channels").json()

    assert body["channels"][0]["pending_pairings"] is None
    assert body["channels"][0]["configured"] is not None, "the engine's half still answers"


def test_an_unreachable_engine_is_a_502(controls_client_as_operator, fake_engine, local_state):
    fake_engine.status = 502
    assert controls_client_as_operator.get("/api/channels").status_code == 502


# ── Verify ──────────────────────────────────────────────────────────────


def test_verify_proxies_the_engines_verdict(controls_client_as_operator, fake_engine, local_state):
    response = controls_client_as_operator.post(
        "/api/channels/slack/verify", json={"target": "C0PLACEHOLDER"}
    )

    assert response.status_code == 200
    assert response.json() == ENGINE_VERIFY
    assert (
        "POST",
        "/api/admin/channels/slack/verify",
        {"target": "C0PLACEHOLDER"},
    ) in fake_engine.calls


def test_verify_writes_one_audit_event_with_identifiers_only(
    controls_client_as_operator, fake_engine, local_state
):
    with patch("routers.channel_access.audited") as audited:
        controls_client_as_operator.post("/api/channels/slack/verify", json={})

    assert audited.call_count == 1
    kwargs = audited.call_args.kwargs
    assert kwargs["action"] == "slack"
    assert kwargs["failed_steps"] == 1
    assert "not_in_channel" not in str(kwargs), "a step detail is not an identifier"


def test_an_unrecognized_channel_name_is_refused_before_the_engine(
    controls_client_as_operator, fake_engine, local_state
):
    response = controls_client_as_operator.post("/api/channels/NOT A NAME/verify", json={})

    assert response.status_code == 422
    assert not fake_engine.calls


def test_a_target_with_a_newline_never_leaves_the_bridge(
    controls_client_as_operator, fake_engine, local_state
):
    """The target reaches a third-party client — ``chat.postMessage``, an SMTP
    envelope — and a newline in one is the header-splitting shape."""
    response = controls_client_as_operator.post(
        "/api/channels/slack/verify", json={"target": "C0PLACEHOLDER\nBcc: someone@example.com"}
    )

    assert response.status_code == 422
    assert not fake_engine.calls
