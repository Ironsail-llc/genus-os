"""Accounts and roles from the Helm: who exists, who may do what.

This is the surface that decides who can sign in to the appliance, so the tests
are written from the attacker's side rather than the form's:

* **Nothing about an account's credentials travels.** ``user_accounts`` rows
  carry ``password_hash``, ``mfa_secret_enc`` and the IdP ``idp_subject``; the
  fixtures below deliberately include all three, so a handler that ever answers
  with the row it was handed fails here rather than in production.
* **The last owner cannot be demoted, disabled, or removed by themselves.** An
  appliance whose only owner is disabled has nobody who can administer it and
  no supported way back in.
* **Another tenant's account does not exist.** A cross-tenant id is 404 and
  never 403 — the difference between the two answers is a confirmation that the
  id is real.
* **The audit trail names identifiers, never addresses.** An audit log is
  exported to a SIEM; an invite's email in the ``detail`` puts a person's
  address in a second system nobody chose.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OPERATOR_ID = "00000000-0000-4000-8000-000000000001"
MEMBER_ID = "00000000-0000-4000-8000-000000000002"
OWNER_ID = "00000000-0000-4000-8000-000000000003"
OTHER_TENANT_ID = "00000000-0000-4000-8000-0000000000ff"

PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$PLACEHOLDERHASHVALUE"
MFA_SECRET = "enc:PLACEHOLDERMFASECRET"
IDP_SUBJECT = "auth0|PLACEHOLDERSUBJECT"

#: Keys that must never appear in any response from this router.
FORBIDDEN_KEYS = ("password_hash", "mfa_secret_enc", "idp_subject")
FORBIDDEN_VALUES = (PASSWORD_HASH, MFA_SECRET, IDP_SUBJECT)


def _row(user_id: str, email: str, role: str = "member", status: str = "active") -> dict:
    """A row shaped like ``SELECT *`` — INCLUDING what must not travel.

    Carrying the three forbidden columns is the point. A fixture that omitted
    them would let a handler answer with ``**row`` and still pass the leak test,
    which is a green light with nothing behind it.
    """
    return {
        "id": user_id,
        "tenant_id": "default",
        "email": email,
        "display_name": email.split("@")[0].title(),
        "role": role,
        "status": status,
        "sso_bound": False,
        "mfa_enabled": False,
        "last_login_at": datetime.now(UTC) - timedelta(days=1),
        "created_at": datetime.now(UTC) - timedelta(days=30),
        "updated_at": datetime.now(UTC),
        "person_id": None,
        "password_hash": PASSWORD_HASH,
        "mfa_secret_enc": MFA_SECRET,
        "idp_issuer": "https://idp.example.com",
        "idp_subject": IDP_SUBJECT,
    }


def _grant(email: str) -> dict:
    """What ``create_binding_grant`` really returns: ``RETURNING *``."""
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": "default",
        "email": email,
        "issuer": None,
        "reason": "",
        "created_by": "operator:someone",
        "created_at": datetime.now(UTC),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "used_at": None,
        "used_by_issuer": None,
        "used_by_subject": IDP_SUBJECT,
        "revoked_at": None,
        "state": "pending",
    }


class FakeAccounts:
    """The account DAL, in memory. Records every call the router makes."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {
            OPERATOR_ID: _row(OPERATOR_ID, "operator@example.com", role="admin"),
            MEMBER_ID: _row(MEMBER_ID, "bob@example.com"),
            OWNER_ID: _row(OWNER_ID, "alice@example.com", role="owner"),
        }
        self.revoked: list[str] = []
        self.created: list[dict] = []
        self.grants: list[dict] = []
        self.duplicate = False
        self.grant_error: Exception | None = None

    # ── reads ────────────────────────────────────────────────────────
    def list_accounts(self, tenant_id: str) -> list[dict]:
        return [dict(row) for row in self.rows.values() if row["tenant_id"] == tenant_id]

    def get_admin_account(self, user_id: str, tenant_id: str) -> dict | None:
        row = self.rows.get(user_id)
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    def active_owner_count(self, tenant_id: str, *, excluding_id: str | None = None) -> int:
        return sum(
            1
            for row in self.rows.values()
            if row["tenant_id"] == tenant_id
            and row["role"] == "owner"
            and row["status"] == "active"
            and row["id"] != excluding_id
        )

    # ── writes ───────────────────────────────────────────────────────
    def create_account(self, **kwargs) -> dict | None:
        if self.duplicate:
            return None
        self.created.append(kwargs)
        created = _row(str(uuid.uuid4()), kwargs["email"], role=kwargs["role"])
        created["status"] = kwargs["status"]
        created["display_name"] = kwargs["display_name"]
        self.rows[created["id"]] = created
        return dict(created)

    def update_account(self, user_id: str, **kwargs) -> dict | None:
        row = self.rows.get(user_id)
        if not row or row["tenant_id"] != kwargs.get("tenant_id"):
            return None
        for field in ("role", "display_name", "status"):
            if kwargs.get(field) is not None:
                row[field] = kwargs[field]
        return dict(row)

    def revoke_user_sessions(self, user_id: str, **kwargs) -> int:
        self.revoked.append(user_id)
        return 2

    def create_binding_grant(self, **kwargs) -> dict:
        if self.grant_error is not None:
            raise self.grant_error
        grant = _grant(kwargs["email"])
        self.grants.append({**kwargs, "id": grant["id"]})
        return grant

    def list_binding_grants(self, tenant_id: str, **kwargs) -> list[dict]:
        return [_grant("bob@example.com"), _grant("someone-else@example.com")]

    def canonical_email(self, email: str | None) -> str:
        return (email or "").strip().lower()


@pytest.fixture
def store(monkeypatch):
    from routers import users

    fake = FakeAccounts()
    for name in (
        "list_accounts",
        "get_admin_account",
        "active_owner_count",
        "create_account",
        "update_account",
        "revoke_user_sessions",
        "create_binding_grant",
        "list_binding_grants",
    ):
        monkeypatch.setattr(users.accounts, name, getattr(fake, name))
    return fake


@pytest.fixture
def operator(_controls_auth_key):
    """An operator session whose ``sub`` is a real account id.

    The shared fixture mints ``operator-1``, which every id check would reject
    as malformed before the rule under test could run — and a self-demotion
    guard that is never reached is not a guard.
    """
    from bridge_service import app
    from fastapi.testclient import TestClient
    from routers._operator import PLATFORM_TENANT

    from robothor.auth import tokens

    token = tokens.issue_access_token(OPERATOR_ID, PLATFORM_TENANT, "owner")
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _walk(payload: object) -> str:
    return json.dumps(payload, default=str)


def _assert_no_credentials(payload: object) -> None:
    rendered = _walk(payload)
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in rendered, f"{key} reached a response body"
    for value in FORBIDDEN_VALUES:
        assert value not in rendered, "a credential value reached a response body"


# ── Roles ───────────────────────────────────────────────────────────────


def test_roles_come_from_the_platforms_own_set(operator, store):
    from robothor.auth.tokens import HUMAN_ROLES, ROLE_DESCRIPTIONS

    body = operator.get("/api/auth/roles").json()

    assert {role["id"] for role in body["roles"]} == set(HUMAN_ROLES)
    assert all(role["description"] == ROLE_DESCRIPTIONS[role["id"]] for role in body["roles"])


def test_a_viewer_cannot_read_the_role_list(controls_client_as_viewer, store):
    """It is a map of what privileges exist on this appliance and it sits under
    ``/api/auth/``, where the middleware's own denial check returns early."""
    assert controls_client_as_viewer.get("/api/auth/roles").status_code == 403


# ── Listing ─────────────────────────────────────────────────────────────


def test_it_lists_the_accounts_in_the_callers_tenant(operator, store):
    body = operator.get("/api/users").json()

    assert {user["email"] for user in body["users"]} == {
        "operator@example.com",
        "bob@example.com",
        "alice@example.com",
    }


def test_the_listing_carries_no_credential_material(operator, store):
    """The fixture rows carry a password hash, an encrypted MFA secret and an
    IdP subject. None of the three may appear, by key or by value."""
    response = operator.get("/api/users")

    _assert_no_credentials(response.json())


def test_the_listing_says_whether_an_account_is_sso_bound_without_saying_to_what(operator, store):
    body = operator.get("/api/users").json()

    assert all(isinstance(user["sso_bound"], bool) for user in body["users"])
    assert IDP_SUBJECT not in _walk(body)


@pytest.mark.parametrize("client_name", ["controls_client_as_viewer", "controls_client_as_service"])
def test_a_non_operator_cannot_list_users(request, client_name, store):
    client = request.getfixturevalue(client_name)
    assert client.get("/api/users").status_code == 403


def test_another_tenants_operator_cannot_list_users(controls_client_as_other_tenant_owner, store):
    assert controls_client_as_other_tenant_owner.get("/api/users").status_code == 403


# ── Invite ──────────────────────────────────────────────────────────────


def test_an_invite_creates_the_account(operator, store):
    response = operator.post(
        "/api/users",
        json={"email": "Carol@Example.com", "role": "member", "display_name": "Carol"},
    )

    assert response.status_code == 201
    assert store.created[0]["email"] == "carol@example.com", "stored canonically, as the CLI does"
    assert store.created[0]["status"] == "invited"
    _assert_no_credentials(response.json())


def test_a_duplicate_email_is_a_409(operator, store):
    store.duplicate = True

    response = operator.post("/api/users", json={"email": "bob@example.com", "role": "member"})

    assert response.status_code == 409


def test_an_unknown_role_is_refused_before_anything_is_written(operator, store):
    response = operator.post("/api/users", json={"email": "carol@example.com", "role": "superuser"})

    assert response.status_code == 422
    assert not store.created


def test_an_address_that_is_not_one_is_refused(operator, store):
    response = operator.post("/api/users", json={"email": "not-an-address", "role": "member"})

    assert response.status_code == 422
    assert not store.created


def test_an_sso_invite_arms_a_binding_grant(operator, store):
    response = operator.post(
        "/api/users", json={"email": "carol@example.com", "role": "member", "sso": True}
    )
    body = response.json()

    assert response.status_code == 201
    assert body["grant"]["id"] == store.grants[0]["id"]
    assert body["grant"]["expires_at"]
    assert store.created[0]["status"] == "active", (
        "jit_provision refuses a non-active row, so an invited account could "
        "never consume the grant the operator just armed"
    )


def test_an_sso_invite_returns_no_grant_internals(operator, store):
    """``create_binding_grant`` returns the whole row — including the IdP
    subject of whoever consumed a previous one. The response is built field by
    field so widening the DAL cannot widen the API."""
    response = operator.post(
        "/api/users", json={"email": "carol@example.com", "role": "member", "sso": True}
    )

    _assert_no_credentials(response.json())
    assert set(response.json()["grant"]) == {"id", "expires_at"}


def test_a_plain_invite_arms_no_grant(operator, store):
    operator.post("/api/users", json={"email": "carol@example.com", "role": "member"})

    assert not store.grants


def test_a_second_owner_is_a_409_rather_than_a_500(operator, store, monkeypatch):
    """Migration 071 puts a partial UNIQUE on ``tenant_id WHERE role='owner'``,
    so the database refuses this however carefully the route counts first. The
    branch exists so the refusal reads as a conflict instead of a traceback —
    and it is exercised here, because an ``except`` clause nothing ever enters
    is the inert control this repo keeps shipping."""
    import psycopg2

    def _conflict(**kwargs):
        raise psycopg2.errors.UniqueViolation("duplicate key value violates uq_user_accounts_owner")

    monkeypatch.setattr("routers.users.accounts.create_account", _conflict)

    response = operator.post("/api/users", json={"email": "carol@example.com", "role": "owner"})

    assert response.status_code == 409
    assert "owner" in response.json()["detail"]


def test_promoting_a_second_owner_is_a_409_too(operator, store, monkeypatch):
    import psycopg2

    def _conflict(*args, **kwargs):
        raise psycopg2.errors.UniqueViolation("duplicate key value violates uq_user_accounts_owner")

    monkeypatch.setattr("routers.users.accounts.update_account", _conflict)

    assert operator.patch(f"/api/users/{MEMBER_ID}", json={"role": "owner"}).status_code == 409


def test_a_database_that_is_down_is_a_503_not_a_500(operator, store, monkeypatch):
    """An operator paged at 3am needs to know whether the appliance crashed or
    Postgres did."""
    import psycopg2

    def _down(*args, **kwargs):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr("routers.users.accounts.list_accounts", _down)

    assert operator.get("/api/users").status_code == 503


def test_an_invite_is_audited_without_the_address(operator, store):
    with patch("routers.users.audited") as audited:
        operator.post("/api/users", json={"email": "carol@example.com", "role": "member"})

    assert audited.called
    assert "carol@example.com" not in str(audited.call_args.kwargs)


# ── Changing an account ─────────────────────────────────────────────────


def test_a_role_change_is_applied(operator, store):
    response = operator.patch(f"/api/users/{MEMBER_ID}", json={"role": "viewer"})

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "viewer"


def test_the_last_active_owner_cannot_be_demoted(operator, store):
    response = operator.patch(f"/api/users/{OWNER_ID}", json={"role": "member"})

    assert response.status_code == 409
    assert "owner" in response.json()["detail"].lower()
    assert store.rows[OWNER_ID]["role"] == "owner"


def test_the_last_active_owner_cannot_be_disabled(operator, store):
    response = operator.patch(f"/api/users/{OWNER_ID}", json={"status": "disabled"})

    assert response.status_code == 409
    assert store.rows[OWNER_ID]["status"] == "active"


def test_an_owner_can_be_demoted_when_another_active_owner_exists(operator, store):
    second = _row("00000000-0000-4000-8000-00000000000a", "dana@example.com", role="owner")
    store.rows[second["id"]] = second

    assert operator.patch(f"/api/users/{OWNER_ID}", json={"role": "member"}).status_code == 200


def test_a_caller_cannot_demote_themselves(operator, store):
    """Not a courtesy. An admin who demotes their own session keeps a token
    carrying the old role until it expires, so the appliance's state and the
    session's claims disagree for the next fifteen minutes."""
    response = operator.patch(f"/api/users/{OPERATOR_ID}", json={"role": "viewer"})

    assert response.status_code == 409
    assert store.rows[OPERATOR_ID]["role"] == "admin"


def test_a_caller_cannot_disable_themselves(operator, store):
    response = operator.patch(f"/api/users/{OPERATOR_ID}", json={"status": "disabled"})

    assert response.status_code == 409
    assert not store.revoked


def test_a_caller_may_still_rename_themselves(operator, store):
    """The self-guard is about authority, not about the row."""
    response = operator.patch(f"/api/users/{OPERATOR_ID}", json={"display_name": "Ops"})

    assert response.status_code == 200


def test_disabling_an_account_revokes_its_sessions(operator, store):
    """Otherwise the disabled account keeps working for up to thirty days: the
    refresh token is what a session is, and nothing about ``status`` removes
    one that is already minted."""
    response = operator.patch(f"/api/users/{MEMBER_ID}", json={"status": "disabled"})

    assert response.status_code == 200
    assert store.revoked == [MEMBER_ID]


def test_a_disable_whose_revoke_fails_is_reported_and_still_audited(operator, store, monkeypatch):
    """The row is disabled and the sessions are not — a half state. It is
    audited before the refusal, because a change that happened and was never
    recorded reads afterwards as one that did not, and the message has to say
    which half is outstanding."""
    import psycopg2

    def _down(*args, **kwargs):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr("routers.users.accounts.revoke_user_sessions", _down)

    with patch("routers.users.audited") as audited:
        response = operator.patch(f"/api/users/{MEMBER_ID}", json={"status": "disabled"})

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]
    assert audited.call_args.kwargs["reason"] == "sessions_not_revoked"


def test_an_unknown_status_is_refused(operator, store):
    response = operator.patch(f"/api/users/{MEMBER_ID}", json={"status": "deleted"})

    assert response.status_code == 422


def test_a_patch_is_audited_with_identifiers_only(operator, store):
    with patch("routers.users.audited") as audited:
        operator.patch(f"/api/users/{MEMBER_ID}", json={"role": "viewer"})

    kwargs = audited.call_args.kwargs
    assert kwargs["action"] == MEMBER_ID
    assert "bob@example.com" not in str(kwargs)


# ── Tenant isolation and id validation ──────────────────────────────────


def test_an_account_in_another_tenant_is_a_404_not_a_403(operator, store):
    """403 confirms the id names something. 404 says nothing at all, which is
    the only answer that does not enumerate another tenant's accounts."""
    foreign = _row(OTHER_TENANT_ID, "eve@example.com")
    foreign["tenant_id"] = "acme-corp"
    store.rows[OTHER_TENANT_ID] = foreign

    assert operator.get(f"/api/users/{OTHER_TENANT_ID}/binding-grants").status_code == 404
    assert (
        operator.patch(f"/api/users/{OTHER_TENANT_ID}", json={"role": "viewer"}).status_code == 404
    )
    assert operator.post(f"/api/users/{OTHER_TENANT_ID}/binding-grant").status_code == 404
    assert foreign["role"] == "member", "a foreign row must not be touched"


@pytest.mark.parametrize(
    "bad_id",
    ["not-a-uuid", "------------------------------------", "a" * 36, "1 OR 1=1"],
)
def test_an_id_that_is_not_a_uuid_is_a_422(operator, store, bad_id):
    """``WHERE id = %s`` on a UUID column turns a typo into a 500. A shape check
    that admits values the column cannot hold is not a shape check."""
    assert operator.patch(f"/api/users/{bad_id}", json={"role": "viewer"}).status_code == 422


# ── Binding grants ──────────────────────────────────────────────────────


def test_a_grant_can_be_armed_for_an_existing_account(operator, store):
    response = operator.post(f"/api/users/{MEMBER_ID}/binding-grant")

    assert response.status_code == 201
    assert set(response.json()["grant"]) == {"id", "expires_at"}
    assert store.grants[0]["email"] == "bob@example.com"
    _assert_no_credentials(response.json())


def test_arming_a_grant_for_an_unusable_account_is_a_409(operator, store):
    from robothor.auth.accounts import GrantTargetError

    store.grant_error = GrantTargetError("account is already bound to an SSO identity")

    assert operator.post(f"/api/users/{MEMBER_ID}/binding-grant").status_code == 409


def test_listing_grants_shows_only_this_accounts_own(operator, store):
    body = operator.get(f"/api/users/{MEMBER_ID}/binding-grants").json()

    assert len(body["grants"]) == 1, "the other tenant member's grant is not this account's"
    _assert_no_credentials(body)


def test_listing_grants_never_carries_the_consuming_identity(operator, store):
    """``sso_binding_grants`` records ``used_by_subject`` — the IdP's own id for
    a person — and the row comes back as ``SELECT *``."""
    body = operator.get(f"/api/users/{MEMBER_ID}/binding-grants").json()

    assert IDP_SUBJECT not in _walk(body)


@pytest.mark.parametrize("client_name", ["controls_client_as_viewer", "controls_client_as_service"])
def test_a_non_operator_cannot_arm_a_grant(request, client_name, store):
    client = request.getfixturevalue(client_name)

    assert client.post(f"/api/users/{MEMBER_ID}/binding-grant").status_code == 403
    assert not store.grants
