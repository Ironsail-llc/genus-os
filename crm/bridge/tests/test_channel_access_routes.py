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
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CODE = "ABC234"
IDENTITY_ID = str(uuid.uuid4())
NATIVE_ID = "U0PLACEHOLDER"

#: What the DAL really stores next to a pending code. Never a projected field.
CODE_HASH = "0" * 64


def _pending_row():
    """The row the DAL hands the router -- INCLUDING what must not travel.

    The first cut of this fixture carried neither ``native_id`` nor
    ``code_hash``, so the one test named for the leak could not see it: adding
    ``"native_id": row.get("native_id")`` to the router's projection returned
    ``None`` and the assertion passed. A leak test whose fixture lacks the thing
    that could leak is a green light with nothing behind it.

    ``list_pending`` projects both away today. A future edit that widens it to
    ``SELECT *`` has to fail here.
    """
    from datetime import UTC, datetime, timedelta

    return {
        "id": str(uuid.uuid4()),
        "channel": "slack",
        "native_id": NATIVE_ID,
        "code_hash": CODE_HASH,
        "display_name": "Alice Example",
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


@pytest.mark.parametrize(
    "spelling",
    [
        f"{{{IDENTITY_ID}}}",
        IDENTITY_ID.replace("-", ""),
        IDENTITY_ID.upper(),
        f"urn:uuid:{IDENTITY_ID}",
    ],
)
def test_an_identity_id_reaches_the_dal_canonically(
    controls_client_as_operator, store, monkeypatch, spelling
):
    """``uuid.UUID`` accepts braces, hyphen-less hex, upper case and the URN
    form, and PostgreSQL's ``uuid`` input accepts most of the same for the SAME
    value — so a validator that hands back what the caller typed lets one row
    be addressed under four different strings. Here that reaches the audit
    ``identity_id`` and the DAL argument; on the users router the identical
    pattern walked past the self-demotion guard (review round 1, I1)."""
    seen: list[str] = []
    monkeypatch.setattr(store.identities, "revoke", lambda ident, **kw: seen.append(ident) or True)

    response = controls_client_as_operator.delete(f"/api/channels/slack/identities/{spelling}")

    assert response.status_code == 200
    assert seen == [IDENTITY_ID]
    assert response.json()["id"] == IDENTITY_ID


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
    assert CODE_HASH not in rendered
    assert "code_hash" not in rendered
    assert "Alice Example" not in rendered
    assert body["pending"][0]["display_name_present"] is True


def test_pending_projects_an_exact_field_set(controls_client_as_operator, store):
    """Named rather than only asserted absent, so widening the projection is a
    decision somebody had to make rather than a diff nobody read."""
    body = controls_client_as_operator.get("/api/channels/slack/pending").json()

    assert set(body["pending"][0]) == {
        "id",
        "channel",
        "expires_at",
        "created_at",
        "display_name_present",
    }


def test_pending_names_the_channel_it_was_asked_about(controls_client_as_operator, store):
    body = controls_client_as_operator.get("/api/channels/slack/pending").json()
    assert body["channel"] == "slack"


def test_identities_listing_returns_the_id_the_delete_route_takes(
    controls_client_as_operator, store
):
    body = controls_client_as_operator.get("/api/channels/slack/identities").json()
    assert body["identities"][0]["id"] == IDENTITY_ID


def test_identities_listing_fingerprints_the_native_id(controls_client_as_operator, store):
    """An operator reviewing bindings needs to tell them apart, not to read a
    workspace's member ids. A leaked operator session could otherwise enumerate
    every bound Slack id in one call; a sha256 prefix distinguishes rows and
    carries none of the value."""
    import hashlib

    body = controls_client_as_operator.get("/api/channels/slack/identities").json()
    row = body["identities"][0]

    assert NATIVE_ID not in str(body)
    assert row["native_id_fingerprint"] == hashlib.sha256(NATIVE_ID.encode()).hexdigest()[:12]
    assert "native_id" not in row


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


def test_the_default_role_is_viewer(controls_client_as_operator, store, monkeypatch):
    """Omitting ``role`` must not grant every tool.

    The seeded ``member`` policy is ``("member", "*", "allow")`` and migration
    088 narrows it only for the ``__default__`` tenant, so the previous default
    was a cap in name only. ``member`` is still accepted -- by name.
    """
    seen = {}

    def _approve(code, **kw):
        seen.update(kw)
        return _identity_row()

    monkeypatch.setattr(store.identities, "approve_pairing", _approve)
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice"}
    )

    assert response.status_code == 200
    assert seen["role"] == "viewer"
    assert response.json()["role"] == "viewer"


def test_member_is_still_accepted_when_named(controls_client_as_operator, store):
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve",
        json={"user_id": "u-alice", "role": "member"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "member"


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


def test_every_refused_approval_writes_a_trail(controls_client_as_operator, store):
    """Both 400 paths, not just the role one. An approval attempt that named
    nobody is still somebody trying to spend a code, and the sibling refusal
    already recorded itself -- one of two arms writing a row is the shape that
    gets read as "this never happened"."""
    for body in ({}, {"user_id": "u-alice", "email": "bob@example.com"}):
        with patch("routers.channel_access.audited") as audited:
            controls_client_as_operator.post(
                f"/api/channels/slack/pairings/{CODE}/approve", json=body
            )
        assert audited.called, f"no audit row for {body}"
        assert audited.call_args.kwargs["status"] == "denied"


def test_an_unknown_code_is_a_404_not_a_500(controls_client_as_operator, store, monkeypatch):
    from robothor.engine.channels.identities import PairingCodeError

    def _boom(*a, **kw):
        raise PairingCodeError("no live code")

    monkeypatch.setattr(store.identities, "approve_pairing", _boom)
    response = controls_client_as_operator.post(
        f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice"}
    )
    assert response.status_code == 404


# ── Malformed input ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "identity_id",
    [
        "-" * 36,  # 36 characters, all hyphens
        "a" * 36,  # 36 hex-ish characters that are not a UUID
        "not-a-uuid",
        "00000000-0000-0000-0000-00000000000g",
        "",
    ],
)
def test_a_malformed_identity_id_is_refused_before_the_database(
    controls_client_as_operator, store, identity_id
):
    """A regex of ``[0-9a-fA-F-]{36}`` matched 36 hyphens, which reached
    ``WHERE id = %s`` on a UUID column and came back as a 500 -- an application
    crash for a caller typo. ``uuid.UUID`` is the only check that means it."""
    with patch("routers.channel_access.identities.revoke") as revoke:
        response = controls_client_as_operator.delete(
            f"/api/channels/slack/identities/{identity_id}"
        )

    # 405 for the empty id: the URL then has a trailing slash and matches no
    # route at all, which is the router refusing it one layer earlier. What
    # matters is that none of these is a 500 and none of them reaches the DAL.
    assert response.status_code in (404, 405, 422)
    revoke.assert_not_called()


@pytest.mark.parametrize("name", ["Slack", "slack channel", "../etc", "x" * 40, ""])
def test_a_malformed_channel_name_is_refused(controls_client_as_operator, store, name):
    response = controls_client_as_operator.get(f"/api/channels/{name}/pending")
    assert response.status_code in (404, 422)


@pytest.mark.parametrize("code", ["abc", "ABC2340", "ABC23I", "O00000", ""])
def test_a_malformed_pairing_code_is_refused_before_the_database(
    controls_client_as_operator, store, code
):
    with patch("routers.channel_access.identities.approve_pairing") as approve:
        response = controls_client_as_operator.post(
            f"/api/channels/slack/pairings/{code}/approve", json={"user_id": "u-alice"}
        )

    assert response.status_code in (404, 422)
    approve.assert_not_called()


def test_a_database_failure_is_a_503_not_a_500(controls_client_as_operator, store, monkeypatch):
    """A database blip on a read must page as a dependency being down, not as
    the appliance having crashed."""
    import psycopg2

    def _down(*a, **kw):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(store.identities, "list_pending", _down)
    response = controls_client_as_operator.get("/api/channels/slack/pending")

    assert response.status_code == 503


# ── Conflicts ───────────────────────────────────────────────────────


def test_a_duplicate_binding_is_a_409_not_a_500(controls_client_as_operator, store, monkeypatch):
    """``tenant_users.user_id`` is globally UNIQUE, so approving a second
    Telegram binding for one person raises UniqueViolation inside the DAL. It
    escaped as a 500; it is a conflict the operator can act on."""
    from robothor.engine.channels.identities import PairingConflictError

    def _conflict(*a, **kw):
        raise PairingConflictError("that user already has a Telegram binding")

    monkeypatch.setattr(store.identities, "approve_pairing", _conflict)
    response = controls_client_as_operator.post(
        f"/api/channels/telegram/pairings/{CODE}/approve", json={"user_id": "u-alice"}
    )

    assert response.status_code == 409
    assert "already has a Telegram binding" in response.json()["detail"]


# ── The tenant gate ─────────────────────────────────────────────────


def test_the_router_is_mounted_with_the_primary_tenant_dependency():
    """Pinned structurally because it cannot be pinned behaviourally here:
    ``PLATFORM_TENANT`` and ``DEFAULT_TENANT`` are the same value in this suite,
    so ``require_operator`` already rejects every request this dependency would.
    Deleting the ``dependencies=[...]`` argument left all 40 tests green."""
    from routers import channel_access

    mounted = [dep.dependency for dep in channel_access.router.dependencies]
    assert channel_access._require_primary_tenant in mounted


def test_the_primary_tenant_dependency_refuses_a_secondary_tenant():
    from fastapi import HTTPException
    from routers import channel_access

    with pytest.raises(HTTPException) as raised:
        channel_access._require_primary_tenant(tenant_id="acme-corp")

    assert raised.value.status_code == 403


# ── Cache invalidation across the process boundary ──────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", f"/api/channels/slack/pairings/{CODE}/approve", {"user_id": "u-alice"}),
        ("post", f"/api/channels/slack/pairings/{CODE}/deny", {}),
        ("delete", f"/api/channels/slack/identities/{IDENTITY_ID}", None),
    ],
)
def test_every_settlement_tells_the_engine_to_drop_its_identity_caches(
    controls_client_as_operator, store, method, path, body
):
    """The row is written in THIS process; the engine's belief about who a
    sender is lives in its own, for up to 300s. Without this call an operator
    watches a revoke succeed and the revoked sender keeps running."""
    with patch("routers.channel_access.engine_request", new_callable=AsyncMock) as engine:
        engine.return_value = (200, {"reloaded": True})
        _call(controls_client_as_operator, method, path, body)

    assert engine.await_count == 1
    assert engine.await_args.args == ("POST", "/api/admin/identities/reload")


def test_a_settlement_survives_an_engine_that_cannot_be_reached(controls_client_as_operator, store):
    """Best effort: the row is already written and the caches expire on their
    own. Failing the approval because the engine is restarting would be worse
    than the staleness this is trying to shorten."""
    with patch("routers.channel_access.engine_request", new_callable=AsyncMock) as engine:
        engine.return_value = (502, {"error": "engine unreachable"})
        response = controls_client_as_operator.post(
            f"/api/channels/slack/pairings/{CODE}/approve", json={"user_id": "u-alice"}
        )

    assert response.status_code == 200


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
