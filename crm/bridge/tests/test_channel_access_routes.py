"""The Helm's half of pairing: see what is waiting, approve it, revoke it.

This router is the *only* network-reachable way to approve a pairing, which is
why every test here is about the gate rather than the happy path. The threat it
answers is specific: a stranger who was handed a six-character code will try to
get it approved, and the surfaces they can reach are the channel itself (which
``robothor/engine/channels/access.py`` proves cannot approve anything) and this
API. So every route asks ``require_operator`` first — no session, a service
token, a non-operator role and an operator of another tenant are all 403 — and
every mutation writes one ``audited`` event carrying identifiers only.

``GET /pending`` gets its own test because it is the one read a compromised
operator session would want: the code itself, or the native id of everyone
currently knocking. It returns neither.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CODE = "ABC234"
IDENTITY_ID = str(uuid.uuid4())
NATIVE_ID = "U0PLACEHOLDER"


def _pending_row():
    from datetime import UTC, datetime, timedelta

    return {
        "id": str(uuid.uuid4()),
        "channel": "slack",
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        "created_at": datetime.now(UTC),
        "display_name_present": True,
    }


def _identity_row():
    from datetime import UTC, datetime

    return {
        "id": IDENTITY_ID,
        "channel": "slack",
        "user_id": "u-alice",
        "native_id": NATIVE_ID,
        "display_name": "Alice Example",
        "role": "member",
        "paired_at": datetime.now(UTC),
        "paired_by": "operator:admin-1",
    }


@pytest.fixture
def store(monkeypatch):
    from routers import channel_access

    monkeypatch.setattr(
        channel_access.identities, "list_pending", lambda *a, **kw: [_pending_row()]
    )
    monkeypatch.setattr(
        channel_access.identities, "list_identities", lambda *a, **kw: [_identity_row()]
    )
    monkeypatch.setattr(
        channel_access.identities, "approve_pairing", lambda *a, **kw: _identity_row()
    )
    monkeypatch.setattr(channel_access.identities, "deny_pairing", lambda *a, **kw: {"id": "x"})
    monkeypatch.setattr(channel_access.identities, "revoke", lambda *a, **kw: True)
    return channel_access


ROUTES = [
    ("get", "/api/channels/slack/pending", None),
    ("post", f"/api/channels/slack/pairings/{CODE}/approve", {"user_id": "u-alice"}),
    ("post", f"/api/channels/slack/pairings/{CODE}/deny", {}),
    ("get", "/api/channels/slack/identities", None),
    ("delete", f"/api/channels/slack/identities/{IDENTITY_ID}", None),
]


def _call(client, method, path, body):
    caller = getattr(client, method)
    return caller(path, json=body) if body is not None else caller(path)


# ── The gate ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_refuses_a_non_operator(controls_client_as_viewer, method, path, body):
    assert _call(controls_client_as_viewer, method, path, body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_refuses_a_service_token(controls_client_as_service, method, path, body):
    assert _call(controls_client_as_service, method, path, body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_refuses_an_operator_of_another_tenant(
    controls_client_as_other_tenant_owner, method, path, body
):
    assert _call(controls_client_as_other_tenant_owner, method, path, body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_refuses_an_unauthenticated_caller(method, path, body):
    from bridge_service import app
    from fastapi.testclient import TestClient

    assert _call(TestClient(app), method, path, body).status_code in (401, 403)


def test_a_refused_caller_approves_nothing(controls_client_as_viewer):
    with patch("routers.channel_access.identities.approve_pairing") as approve:
        controls_client_as_viewer.post(
            f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice"}
        )
    approve.assert_not_called()


# ── What the reads may say ──────────────────────────────────────────


def test_pending_never_returns_a_native_id_or_a_code(controls_client_as_operator, store):
    body = controls_client_as_operator.get("/api/channels/slack/pending").json()

    rendered = str(body)
    assert NATIVE_ID not in rendered
    assert CODE not in rendered
    assert "code_hash" not in rendered
    assert body["pending"][0]["display_name_present"] is True


def test_pending_names_the_channel_it_was_asked_about(controls_client_as_operator, store):
    body = controls_client_as_operator.get("/api/channels/slack/pending").json()
    assert body["channel"] == "slack"


def test_identities_listing_returns_the_id_the_delete_route_takes(
    controls_client_as_operator, store
):
    body = controls_client_as_operator.get("/api/channels/slack/identities").json()
    assert body["identities"][0]["id"] == IDENTITY_ID


# ── Approval ────────────────────────────────────────────────────────


def test_approve_passes_an_operator_actor(controls_client_as_operator, store, monkeypatch):
    seen = {}

    def _approve(code, **kw):
        seen.update(kw, code=code)
        return _identity_row()

    monkeypatch.setattr(store.identities, "approve_pairing", _approve)
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice"}
    )

    assert response.status_code == 200
    assert seen["code"] == CODE
    assert seen["actor"].startswith("operator:")
    assert seen["channel"] == "slack"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_approve_refuses_a_privileged_role(controls_client_as_operator, store, role):
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice", "role": role}
    )
    assert response.status_code == 400


def test_approve_refuses_when_neither_user_nor_email_is_named(controls_client_as_operator, store):
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve", json={}
    )
    assert response.status_code == 400


def test_an_unknown_code_is_a_404_not_a_500(controls_client_as_operator, store, monkeypatch):
    from robothor.engine.channels.identities import PairingCodeError

    def _boom(*a, **kw):
        raise PairingCodeError("no live code")

    monkeypatch.setattr(store.identities, "approve_pairing", _boom)
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice"}
    )
    assert response.status_code == 404


# ── The trail ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", f"/api/channels/slack/pairings/{CODE}/approve", {"user_id": "u-alice"}),
        ("post", f"/api/channels/slack/pairings/{CODE}/deny", {}),
        ("delete", f"/api/channels/slack/identities/{IDENTITY_ID}", None),
    ],
)
def test_every_mutation_is_audited_with_identifiers_only(
    controls_client_as_operator, store, method, path, body
):
    with patch("routers.channel_access.audited") as audited:
        _call(controls_client_as_operator, method, path, body)

    assert audited.called
    _, kwargs = audited.call_args
    rendered = str(kwargs)
    assert CODE not in rendered
    assert NATIVE_ID not in rendered
    assert "Alice Example" not in rendered
