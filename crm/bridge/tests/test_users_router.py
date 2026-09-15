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
#: Hex letters on purpose: an upper-case spelling of this id is a different
#: string and the same UUID, which is the gap I1 walked through.
ADMIN_ID = "00000000-0000-4000-8000-00000000000a"
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


def _canonical(user_id: object) -> str:
    """Resolve an id the way PostgreSQL's ``uuid`` column does.

    The first version of this fake keyed ``rows`` on the exact string it was
    given, so ``{0000…}``, ``0000…`` (no hyphens) and the canonical spelling
    were three different accounts — which is not what the database does, and is
    why the self-demotion guard could be walked past with a green suite behind
    it (review round 1, I1). A fake that is more literal-minded than the real
    store hides exactly the bugs the real store would expose.
    """
    try:
        return str(uuid.UUID(str(user_id)))
    except (ValueError, AttributeError, TypeError):
        return str(user_id)


class FakeAccounts:
    """The account DAL, in memory. Records every call the router makes."""

    def __init__(self) -> None:
        # Exactly ONE owner row, because migration 071's partial unique index
        # allows exactly one. A fake with two owners would make the sole-owner
        # guard untestable and every demotion look safe.
        self.rows: dict[str, dict] = {
            OWNER_ID: _row(OWNER_ID, "alice@example.com", role="owner"),
            ADMIN_ID: _row(ADMIN_ID, "admin@example.com", role="admin"),
            OPERATOR_ID: _row(OPERATOR_ID, "operator@example.com", role="admin"),
            MEMBER_ID: _row(MEMBER_ID, "bob@example.com"),
        }
        self.revoked: list[str] = []
        self.created: list[dict] = []
        self.grants: list[dict] = []
        self.duplicate = False
        self.grant_error: Exception | None = None

    def set_owner_status(self, status: str) -> None:
        """Put the owner row in a state a CLI-built instance really has.

        ``genus user add --role owner`` writes ``status='invited'`` and leaves
        it there until somebody runs ``genus user set-password``, so ``invited``
        is the DEFAULT owner state on a fresh instance rather than an exotic
        one. The first version of this fake could only express ``active``, so
        every guard keyed on "an active owner" tested green against the one
        state the guard did cover.
        """
        self.rows[OWNER_ID]["status"] = status

    # ── reads ────────────────────────────────────────────────────────
    def list_accounts(self, tenant_id: str) -> list[dict]:
        return [dict(row) for row in self.rows.values() if row["tenant_id"] == tenant_id]

    def get_admin_account(self, user_id: str, tenant_id: str) -> dict | None:
        row = self.rows.get(_canonical(user_id))
        return dict(row) if row and row["tenant_id"] == tenant_id else None

    def owner_count(
        self, tenant_id: str, *, excluding_id: str | None = None, active_only: bool = True
    ) -> int:
        excluded = _canonical(excluding_id) if excluding_id else None
        return sum(
            1
            for row in self.rows.values()
            if row["tenant_id"] == tenant_id
            and row["role"] == "owner"
            and (row["status"] == "active" or not active_only)
            and row["id"] != excluded
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
        row = self.rows.get(_canonical(user_id))
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
        grant["issuer"] = kwargs.get("issuer")
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
        "owner_count",
        "create_account",
        "update_account",
        "revoke_user_sessions",
        "create_binding_grant",
        "list_binding_grants",
    ):
        monkeypatch.setattr(users.accounts, name, getattr(fake, name))
    return fake


def _session(user_id: str, role: str):
    """A verified operator session for one account id and role.

    The shared conftest fixture mints ``operator-1``, which every id check would
    reject as malformed before the rule under test could run — and a
    self-demotion guard that is never reached is not a guard. The ROLE matters
    too: it is the caller's own authority, and the rules added in review round 1
    turn on the difference between an owner and an admin.
    """
    from bridge_service import app
    from fastapi.testclient import TestClient
    from routers._operator import PLATFORM_TENANT

    from robothor.auth import tokens

    token = tokens.issue_access_token(user_id, PLATFORM_TENANT, role)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture
def operator(_controls_auth_key):
    """The owner's own session — the caller with the most authority there is."""
    return _session(OWNER_ID, "owner")


@pytest.fixture
def admin(_controls_auth_key):
    """An admin session — an operator, but not the owner.

    The caller every authority rule below is aimed at: ``require_operator``
    admits owner and admin alike, so nothing but an explicit role check keeps
    an admin from taking the owner slot.
    """
    return _session(ADMIN_ID, "admin")


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
        "admin@example.com",
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
    # ``issuer`` is the IdP the grant is pinned to — appliance configuration and
    # public metadata, not a person's identifier. ``used_by_subject`` is the
    # person's, and is what must never appear.
    assert set(response.json()["grant"]) == {"id", "expires_at", "issuer"}


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


@pytest.mark.parametrize("owner_status", ["active", "invited", "disabled"])
def test_the_only_owner_cannot_be_demoted_whatever_its_status(operator, store, owner_status):
    """The guard used to require ``status == "active"``, and an owner row is
    ``invited`` on every CLI-built instance until somebody sets a password
    (``genus user add`` writes that status). An unprotected owner row is an
    unprotected OWNER SLOT: migration 071 caps a tenant at one owner, so
    demoting the incumbent frees the slot for whoever asks next."""
    store.set_owner_status(owner_status)

    response = operator.patch(f"/api/users/{OWNER_ID}", json={"role": "member"})

    assert response.status_code == 409
    assert "owner" in response.json()["detail"].lower()
    assert store.rows[OWNER_ID]["role"] == "owner"


@pytest.mark.parametrize("owner_status", ["active", "invited", "disabled"])
def test_the_only_owner_cannot_be_disabled_whatever_its_status(operator, store, owner_status):
    store.set_owner_status(owner_status)

    response = operator.patch(f"/api/users/{OWNER_ID}", json={"status": "disabled"})

    assert response.status_code == 409
    assert store.rows[OWNER_ID]["status"] == owner_status


def test_an_owner_can_be_demoted_when_a_second_owner_row_exists(operator, store):
    """The count is the belt to the role check's braces, and it counts owner
    ROWS rather than active owners — an invited owner still holds the slot.
    The owner demotes the OTHER owner here; demoting themselves is the sole-
    owner case above, and would be refused for that reason rather than this."""
    second_id = "00000000-0000-4000-8000-00000000000d"
    second = _row(second_id, "dana@example.com", role="owner")
    second["status"] = "invited"
    store.rows[second_id] = second

    assert operator.patch(f"/api/users/{second_id}", json={"role": "member"}).status_code == 200
    assert store.rows[second_id]["role"] == "member"


# ── C1: an admin must not be able to take the owner slot ────────────────


@pytest.mark.parametrize("owner_status", ["active", "invited", "disabled"])
def test_an_admin_cannot_demote_the_owner(admin, store, owner_status):
    """Half of the two-PATCH escalation chain: demote the incumbent to free
    migration 071's single owner slot, then take it."""
    store.set_owner_status(owner_status)

    response = admin.patch(f"/api/users/{OWNER_ID}", json={"role": "member"})

    assert response.status_code == 403
    assert store.rows[OWNER_ID]["role"] == "owner"


@pytest.mark.parametrize("owner_status", ["active", "invited", "disabled"])
def test_an_admin_cannot_disable_the_owner(admin, store, owner_status):
    store.set_owner_status(owner_status)

    assert admin.patch(f"/api/users/{OWNER_ID}", json={"status": "disabled"}).status_code == 403
    assert store.rows[OWNER_ID]["status"] == owner_status


@pytest.mark.parametrize("owner_status", ["invited", "disabled"])
def test_the_whole_escalation_chain_is_refused_at_both_steps(admin, store, owner_status):
    """The probe from review round 1, end to end: demote the non-active owner,
    then promote yourself into the slot it freed. Both halves must refuse, and
    no row may change."""
    store.set_owner_status(owner_status)

    demote = admin.patch(f"/api/users/{OWNER_ID}", json={"role": "member"})
    promote = admin.patch(f"/api/users/{ADMIN_ID}", json={"role": "owner"})

    assert demote.status_code == 403
    assert promote.status_code == 403
    assert store.rows[OWNER_ID]["role"] == "owner"
    assert store.rows[ADMIN_ID]["role"] == "admin"


def test_an_admin_cannot_promote_anybody_to_owner(admin, store):
    response = admin.patch(f"/api/users/{MEMBER_ID}", json={"role": "owner"})

    assert response.status_code == 403
    assert store.rows[MEMBER_ID]["role"] == "member"


def test_an_admin_cannot_invite_an_owner(admin, store):
    response = admin.post("/api/users", json={"email": "mallory@example.com", "role": "owner"})

    assert response.status_code == 403
    assert not store.created


def test_a_refused_authority_change_is_audited(admin, store):
    """An admin probing for the seam leaves a trail. The denial arms of
    ``invite_user`` and ``update_user`` already audited; these are the two that
    did not exist."""
    with patch("routers.users.audited") as audited:
        admin.patch(f"/api/users/{MEMBER_ID}", json={"role": "owner"})

    assert audited.call_args.kwargs["status"] == "denied"
    assert audited.call_args.kwargs["reason"] == "grant_owner"


def test_an_admin_may_still_administer_everybody_else(admin, store):
    """The rule is about the owner and the owner role, not about admins being
    second-class: an admin still runs the fleet of ordinary accounts."""
    assert admin.patch(f"/api/users/{MEMBER_ID}", json={"role": "viewer"}).status_code == 200
    assert admin.patch(f"/api/users/{OWNER_ID}", json={"display_name": "Alice"}).status_code == 200


def test_a_caller_cannot_demote_themselves(admin, store):
    """Not a courtesy. An admin who demotes their own session keeps a token
    carrying the old role until it expires, so the appliance's state and the
    session's claims disagree for the next fifteen minutes."""
    response = admin.patch(f"/api/users/{ADMIN_ID}", json={"role": "viewer"})

    assert response.status_code == 409
    assert store.rows[ADMIN_ID]["role"] == "admin"


@pytest.mark.parametrize(
    "spelling",
    [
        "{00000000-0000-4000-8000-00000000000a}",
        "0000000000004000800000000000000a",
        "00000000-0000-4000-8000-00000000000A",
        "urn:uuid:00000000-0000-4000-8000-00000000000a",
    ],
)
def test_the_self_guard_survives_every_spelling_of_the_callers_own_id(admin, store, spelling):
    """``uuid.UUID`` accepts braces, hyphen-less hex, upper case and the URN
    form; PostgreSQL's ``uuid`` input accepts most of the same. So the row was
    found while ``target_id == caller_id`` — a raw string compare — was not,
    and a stated control was walked past by a text transformation. That is this
    project's canonical inert-control shape."""
    response = admin.patch(f"/api/users/{spelling}", json={"status": "disabled"})

    assert response.status_code == 409
    assert store.rows[ADMIN_ID]["status"] == "active"
    assert not store.revoked


def test_an_id_reaches_the_dal_and_the_audit_canonically(operator, store):
    """The audit ``action`` and the ``revoke_user_sessions`` argument used to be
    whatever the caller typed rather than an id."""
    with patch("routers.users.audited") as audited:
        response = operator.patch(
            f"/api/users/{{{MEMBER_ID.upper()}}}", json={"status": "disabled"}
        )

    assert response.status_code == 200
    assert store.revoked == [MEMBER_ID]
    assert audited.call_args.kwargs["action"] == MEMBER_ID


def test_a_caller_cannot_disable_themselves(admin, store):
    response = admin.patch(f"/api/users/{ADMIN_ID}", json={"status": "disabled"})

    assert response.status_code == 409
    assert not store.revoked


def test_a_caller_may_still_rename_themselves(admin, store):
    """The self-guard is about authority, not about the row."""
    response = admin.patch(f"/api/users/{ADMIN_ID}", json={"display_name": "Ops"})

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


def test_a_role_change_records_the_role_it_replaced(operator, store):
    """Recording only the new role makes an owner→member demotion and a
    member→member no-op the same row in the SIEM — the wrong side of exactly
    the investigation this surface would be investigated for."""
    with patch("routers.users.audited") as audited:
        operator.patch(f"/api/users/{MEMBER_ID}", json={"role": "viewer"})

    kwargs = audited.call_args.kwargs
    assert (kwargs["previous_role"], kwargs["role"]) == ("member", "viewer")


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
    assert set(response.json()["grant"]) == {"id", "expires_at", "issuer"}
    assert store.grants[0]["email"] == "bob@example.com"
    _assert_no_credentials(response.json())


# ── C2: the owner's account is not an admin's to bind ───────────────────


def test_an_admin_cannot_arm_a_binding_grant_on_the_owner(admin, store):
    """``bootstrap_owner_account`` leaves the owner active, with no password and
    no IdP binding — which is exactly the shape ``create_binding_grant``
    accepts. Consuming the grant binds the presenter's identity onto the owner
    account, so an unguarded route here is the second path to owner."""
    response = admin.post(f"/api/users/{OWNER_ID}/binding-grant")

    assert response.status_code == 403
    assert not store.grants


def test_an_admin_cannot_invite_an_sso_owner_either(admin, store):
    response = admin.post(
        "/api/users", json={"email": "mallory@example.com", "role": "owner", "sso": True}
    )

    assert response.status_code == 403
    assert not store.grants
    assert not store.created


def test_the_owner_may_arm_a_grant_on_their_own_account(operator, store):
    assert operator.post(f"/api/users/{OWNER_ID}/binding-grant").status_code == 201


def test_a_refused_grant_is_audited(admin, store):
    """An admin probing which accounts are SSO-bindable left no trail at all."""
    with patch("routers.users.audited") as audited:
        admin.post(f"/api/users/{OWNER_ID}/binding-grant")

    assert audited.call_args.kwargs["status"] == "denied"


def test_a_grant_is_pinned_to_the_one_configured_issuer(operator, store, monkeypatch):
    """``genus auth grant-binding --issuer`` exists because an UNPINNED grant is
    spendable by a verified claim from ANY allowlisted issuer. Where the
    appliance has exactly one, there is no reason to leave it open."""
    monkeypatch.setattr("routers.users._configured_issuers", lambda: ["https://idp.example.com"])

    response = operator.post(f"/api/users/{MEMBER_ID}/binding-grant")

    assert store.grants[0]["issuer"] == "https://idp.example.com"
    assert response.json()["grant"]["issuer"] == "https://idp.example.com"


@pytest.mark.parametrize("issuers", [[], ["https://a.example.com", "https://b.example.com"]])
def test_a_grant_is_left_unpinned_when_the_instance_has_no_single_issuer(
    operator, store, monkeypatch, issuers
):
    """Zero configured issuers means the pin would name nothing; two means
    pinning one of them would refuse a sign-in the operator expects to work.
    The grant is still bounded by the email, the one hour and its single use."""
    monkeypatch.setattr("routers.users._configured_issuers", lambda: issuers)

    operator.post(f"/api/users/{MEMBER_ID}/binding-grant")

    assert store.grants[0]["issuer"] is None


def test_arming_a_grant_for_an_unusable_account_is_a_409(operator, store):
    from robothor.auth.accounts import GrantTargetError

    store.grant_error = GrantTargetError("account is already bound to an SSO identity")

    assert operator.post(f"/api/users/{MEMBER_ID}/binding-grant").status_code == 409


def test_a_refused_grant_never_logs_the_address(operator, store, caplog):
    """``create_binding_grant``'s own message is
    ``f"no account with email {email!r} …"``, and it is reachable if the row
    goes away between the load and the call. This module's header forbids an
    address reaching a second system; the application log is one."""
    import logging

    from robothor.auth.accounts import GrantTargetError

    store.grant_error = GrantTargetError("no account with email 'bob@example.com' in tenant")

    with caplog.at_level(logging.DEBUG):
        operator.post(f"/api/users/{MEMBER_ID}/binding-grant")

    assert "bob@example.com" not in caplog.text


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
