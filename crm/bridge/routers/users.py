"""Accounts and roles — who may sign in to this appliance, and as what.

Before this, an account existed because somebody ran ``genus user add`` over
ssh, and the only way to see the fleet of them was ``genus user list`` on the
box. That is the same gap the provider surface had: the appliance can be
administered, but only by the person holding a shell.

The rules this module is built around, in the order a reviewer should check
them:

* **Every route is ``require_operator`` first line.** Reads included: the
  listing enumerates every account on the appliance and what each one may do,
  which is the map an attacker holding a member session wants most.
* **A credential never travels.** ``user_accounts`` rows carry
  ``password_hash``, ``mfa_secret_enc`` and ``idp_subject``; the DAL projects
  them away and every response here is assembled field by field, so widening
  one of the two cannot widen the API. Same shape as ``providers.py``: a
  secret goes IN and never comes OUT.
* **A caller cannot hand itself authority it does not have.** ``owner`` is
  granted and taken away only by an owner, the owner ROW is only an owner's to
  demote, disable or bind to an identity provider, and the tenant's only owner
  cannot be demoted or disabled at all. Without the first two rules an admin
  reached owner in two PATCHes: migration 071 caps a tenant at one owner row,
  so demoting the incumbent — which on a CLI-built instance is ``invited``, not
  ``active`` — frees the slot for whoever asks next.
* **A caller cannot drop its OWN authority.** The session keeps its old claims
  until the token expires, so the appliance's state and the session's would
  disagree for fifteen minutes. That guard compares account ids, so every id is
  canonicalised at the boundary: ``uuid.UUID`` accepts braces, hyphen-less hex
  and upper case, and a raw string compare on the caller's spelling is a
  control a text transformation walks past.
* **Another tenant's account does not exist.** Every query is scoped by the
  caller's verified tenant and a miss is 404, never 403: the difference between
  those two answers is a confirmation that the id is real.
* **The audit trail names identifiers, never addresses.** ``robothor.audit`` is
  exported to a SIEM; an invite's email address in a ``detail`` field puts a
  person's address into a second system nobody chose to put it in.

Every route is ``def``, not ``async def``: all the work is psycopg2, which
belongs in FastAPI's worker threadpool rather than on the event loop — see
``crm/bridge/tests/test_route_concurrency.py``, where the absence of these
paths is the assertion.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Literal

import psycopg2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from robothor.auth import accounts
from robothor.auth.tokens import HUMAN_ROLES, ROLE_DESCRIPTIONS
from routers._audit import audited
from routers._operator import require_operator

logger = logging.getLogger(__name__)

#: No prefix: this router owns ``/api/users`` and the one role read the sign-in
#: surface's own router has no business serving. Same shape as ``providers.py``,
#: which spans ``/api/providers`` and ``/api/models`` for the same reason — the
#: file boundary follows the feature, not the URL.
router = APIRouter(tags=["users"])

#: How long a binding grant armed from the Helm lives. The CLI's default is
#: fifteen minutes, which assumes the operator is standing next to the person
#: signing in. An invite is handed over out of band — this platform sends no
#: invite mail — so fifteen minutes would mean re-arming it on nearly every
#: use, and an operator who has to repeat a step eventually automates it badly.
#: One hour, one shot, pinned to one address: consuming it still requires a
#: VERIFIED SSO claim for exactly that email from an allowlisted issuer.
BINDING_GRANT_TTL_SECONDS = 3600

#: The one role that is not an ordinary role. It is the tenant's single
#: administrative slot (migration 071's ``uq_user_accounts_owner``), the account
#: ``bootstrap_owner_account`` seeds, and the one whose loss has no recovery
#: path short of a shell on the box.
OWNER_ROLE = "owner"

#: Deliberately loose. This is a shape check to catch a typed mistake before it
#: becomes a row nobody can sign in as; the authority on whether an address
#: exists is the identity provider, and a stricter pattern here would refuse
#: valid addresses that it accepts.
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]{1,63}(?:\.[^@\s.]{1,63})+$")

_MAX_EMAIL_CHARS = 320
_MAX_NAME_CHARS = 200


class InviteRequest(BaseModel):
    """One account on its way in."""

    email: str = Field(min_length=3, max_length=_MAX_EMAIL_CHARS)
    role: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, max_length=_MAX_NAME_CHARS)
    #: Arm a one-shot SSO binding grant alongside the account. Without one, an
    #: account with no password can never sign in at all — ``jit_provision``
    #: refuses to bind an IdP identity to a pre-existing row on an email match
    #: alone, which is the control that stops a verified email claim adopting a
    #: privileged account.
    sso: bool = False


class AccountPatch(BaseModel):
    """What an operator may change about an existing account.

    Not here, deliberately: the email address (it is the account's identity and
    the key every binding grant is armed against), the SSO binding (revoking
    one is ``genus auth``'s job today) and anything to do with a password or a
    second factor — those are the account holder's, and the reset paths are
    ``genus user set-password`` / ``genus user mfa-reset``.
    """

    role: str | None = Field(default=None, max_length=32)
    display_name: str | None = Field(default=None, max_length=_MAX_NAME_CHARS)
    status: Literal["active", "disabled"] | None = None


def _identity(request: Request) -> tuple[str, str, str]:
    """``(caller account id, tenant, caller role)`` from the VERIFIED session.

    Only ever called after ``require_operator``, which is spelled out in each
    handler rather than folded in here: ``test_mutations_are_gated.py`` walks
    the assembled app and reads each handler's own source for that call, so a
    gate hidden behind a helper would be a gate that guard cannot see.

    The tenant comes from the token, never from ``X-Tenant-Id``: that header is
    the unverified legacy fallback, and every query below is scoped by whatever
    this returns. The ROLE comes from the token too, and is the caller's own
    authority — ``require_operator`` admits owner and admin alike, so nothing
    but the rules below separates them.
    """
    auth = request.state.auth
    return str(auth.user_id), str(auth.tenant_id), str(auth.role)


def _account_id(value: str) -> str:
    """An account id in its CANONICAL form, or 422.

    ``uuid.UUID`` and not a regex, for the reason ``channel_access._identity_id``
    spells out: ``WHERE id = %s`` on a UUID column turns a caller's typo into a
    500, and a shape check that admits values the column cannot hold is not a
    shape check.

    Returning ``str(uuid.UUID(...))`` rather than what the caller typed is the
    other half, and it is load-bearing: ``uuid.UUID`` accepts ``{braces}``,
    hyphen-less hex, upper case and ``urn:uuid:``, and PostgreSQL's ``uuid``
    input accepts most of the same spellings for the SAME value. So the row was
    found while ``target_id == caller_id`` — a raw string compare — was not,
    and the self-demotion guard could be walked past by retyping one's own id
    in braces. The audit ``action`` and the ``revoke_user_sessions`` argument
    were whatever the caller typed, too.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="not an account id") from None


def _configured_issuers() -> list[str]:
    """The IdPs this appliance accepts, from the settings model.

    Through ``get_settings()`` rather than ``os.environ``: the env-read ratchet
    in ``tests/test_settings_registry.py`` counts raw reads, and a setting only
    reachable by knowing a variable name is one ``genus config`` cannot show.
    """
    from robothor.settings import get_settings

    raw = get_settings().auth.oidc_issuers or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _issuer_pin() -> str | None:
    """Which issuer to pin a binding grant to, if the answer is unambiguous.

    ``genus auth grant-binding --issuer`` exists because an unpinned grant can
    be consumed by a verified claim for that address from ANY allowlisted
    issuer. Where the appliance has exactly one, pinning costs nothing and
    narrows the grant to the IdP the operator actually runs.

    ``None`` for zero (the pin would name nothing) and for two or more (pinning
    one of them would refuse a sign-in the operator expects to work). The grant
    is still bounded by the address, the TTL and its single use.
    """
    issuers = _configured_issuers()
    return issuers[0] if len(issuers) == 1 else None


def _drops_authority(new_role: str | None, new_status: str | None) -> bool:
    """Whether this change takes authority AWAY from the account it names."""
    return (new_role is not None and new_role != OWNER_ROLE) or new_status == "disabled"


def _refuse_an_authority_change_the_caller_cannot_make(
    request: Request,
    *,
    caller_role: str,
    event: str,
    action: str,
    target_role: str | None = None,
    new_role: str | None = None,
    new_status: str | None = None,
) -> None:
    """The one rule about who may move the OWNER role around.

    One function rather than a clause in each handler because the two halves
    are only dangerous together, and the first version had neither: promoting
    *to* owner was not treated as an authority change at all, and the guard on
    the owner row only fired when that row was ``active``. Migration 071 caps a
    tenant at one owner row, so an admin demoted the (``invited``) incumbent and
    took the slot it freed — two PATCHes, no other check in the way.

    ``require_operator`` cannot express this: it admits owner and admin alike,
    which is correct for every other account on the appliance.

    403 rather than 409: this is about the CALLER's authority, not about the
    appliance's state, and the same request from the owner succeeds.
    """
    if new_role == OWNER_ROLE and caller_role != OWNER_ROLE:
        audited(request, event, action=action, status="denied", reason="grant_owner")
        raise HTTPException(
            status_code=403,
            detail="only an owner may grant the owner role",
        )
    if (
        target_role == OWNER_ROLE
        and caller_role != OWNER_ROLE
        and _drops_authority(new_role, new_status)
    ):
        audited(request, event, action=action, status="denied", reason="demote_owner")
        raise HTTPException(
            status_code=403,
            detail="only an owner may demote or disable the owner account",
        )


def _role(value: str) -> str:
    if value not in HUMAN_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of: {', '.join(sorted(HUMAN_ROLES))}",
        )
    return value


def _email(value: str) -> str:
    clean = accounts.canonical_email(value)
    if not clean or len(clean) > _MAX_EMAIL_CHARS or not _EMAIL.match(clean):
        raise HTTPException(status_code=422, detail="that is not an email address")
    return clean


def _unavailable(exc: BaseException) -> HTTPException:
    """A database that is not answering is a dependency being down, not a bug.

    503 and not 500, for the reason ``channel_access._unavailable`` gives: an
    operator paged at 3am needs to know whether the appliance crashed or
    Postgres did.
    """
    logger.warning("a users route could not reach the database: %s", type(exc).__name__)
    return HTTPException(status_code=503, detail="the account store is unavailable")


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _account_payload(row: dict[str, Any]) -> dict[str, Any]:
    """One account, field by field.

    Never ``{**row}``. The DAL already projects the credential columns away,
    and this is the second lock: a future ``SELECT *`` in the DAL, or a column
    added to the table, cannot widen what an operator's browser receives.
    """
    return {
        "id": str(row["id"]),
        "email": row.get("email"),
        "display_name": row.get("display_name"),
        "role": row.get("role"),
        "status": row.get("status"),
        "sso_bound": bool(row.get("sso_bound")),
        "mfa_enabled": bool(row.get("mfa_enabled")),
        "last_login_at": _iso(row.get("last_login_at")),
        "created_at": _iso(row.get("created_at")),
    }


def _grant_payload(row: dict[str, Any]) -> dict[str, Any]:
    """A freshly armed grant: what it is, when it dies, and what may spend it.

    Three fields, and this is not laziness. ``sso_binding_grants`` has no secret
    column — the grant IS the row, consumed by the next verified SSO claim for
    that address — but it does record ``used_by_subject``, an identity
    provider's own id for a person, and ``create_binding_grant`` returns the
    whole row. Naming the fields that travel means widening the DAL cannot
    widen this. ``issuer`` is appliance configuration and already public
    metadata (``GET /api/auth/methods`` serves the list unauthenticated), and
    the operator needs it: it is the difference between "any configured IdP can
    spend this" and "only that one".
    """
    return {
        "id": str(row["id"]),
        "expires_at": _iso(row.get("expires_at")),
        "issuer": row.get("issuer"),
    }


def _grant_listing_payload(row: dict[str, Any]) -> dict[str, Any]:
    """One grant in the history, for an operator auditing who armed what."""
    return {
        "id": str(row["id"]),
        "state": row.get("state"),
        # Which IdP the grant is pinned to, if any: an appliance-level setting,
        # not a person's identifier. ``used_by_subject`` and ``used_by_issuer``
        # are the person's, and are absent.
        "issuer": row.get("issuer"),
        "created_by": row.get("created_by"),
        "created_at": _iso(row.get("created_at")),
        "expires_at": _iso(row.get("expires_at")),
        "used_at": _iso(row.get("used_at")),
        "revoked_at": _iso(row.get("revoked_at")),
    }


def _load(user_id: str, tenant_id: str) -> dict[str, Any]:
    """One account in the caller's tenant, or 404.

    404 and never 403 for a row in another tenant. A 403 would confirm the id
    names a real account, which is the whole of what an enumeration needs.
    """
    try:
        row = accounts.get_admin_account(user_id, tenant_id)
    except (psycopg2.Error, OSError) as exc:
        raise _unavailable(exc) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="no account with that id")
    return row


def _arm_grant(request: Request, email: str, tenant_id: str, actor: str) -> dict[str, Any]:
    """Arm one binding grant, turning the DAL's refusals into answers.

    ``GrantTargetError`` is a 409 and its message does NOT travel: two of the
    three refusals name the account's own address, and the third says nothing
    the operator cannot see on the page already.
    """
    try:
        return accounts.create_binding_grant(
            tenant_id=tenant_id,
            email=email,
            ttl_seconds=BINDING_GRANT_TTL_SECONDS,
            reason="armed from the Helm",
            created_by=actor,
            issuer=_issuer_pin(),
        )
    except accounts.GrantTargetError as exc:
        # The CLASS, never the message: ``create_binding_grant``'s own text is
        # ``f"no account with email {email!r} …"``, and this module's header
        # forbids an address reaching a second system — an application log is
        # one of those.
        logger.info("binding grant refused for an account in %s: %s", tenant_id, type(exc).__name__)
        raise HTTPException(
            status_code=409,
            detail=(
                "that account cannot consume a binding grant — it is already "
                "bound to an identity provider, or it is not active"
            ),
        ) from exc
    except (psycopg2.Error, OSError) as exc:
        raise _unavailable(exc) from exc


# ── Roles ───────────────────────────────────────────────────────────────


@router.get("/api/auth/roles")
def list_roles(request: Request) -> dict[str, Any]:
    """Every role an account may hold, with the sentence that explains it.

    Served from ``tokens.HUMAN_ROLES`` rather than a list in the dashboard: a
    picker that offers a role the token layer does not know creates an account
    that cannot sign in, and this file is how the two stay one fact.
    """
    require_operator(request)
    return {
        "roles": [
            {"id": role, "description": ROLE_DESCRIPTIONS[role]} for role in sorted(HUMAN_ROLES)
        ]
    }


# ── Accounts ────────────────────────────────────────────────────────────


@router.get("/api/users")
def list_users(request: Request) -> dict[str, Any]:
    """Every account in the caller's tenant. No credentials, ever."""
    require_operator(request)
    _, tenant_id, _ = _identity(request)
    try:
        rows = accounts.list_accounts(tenant_id)
    except (psycopg2.Error, OSError) as exc:
        raise _unavailable(exc) from exc
    return {"users": [_account_payload(row) for row in rows], "count": len(rows)}


@router.post("/api/users", status_code=201)
def invite_user(body: InviteRequest, request: Request) -> JSONResponse:
    """Create one sign-in account.

    **Narrower than ``genus user add``, deliberately.** That command writes a
    whole identity: a ``crm_people`` row, a ``tenant_users`` membership, a
    ``contact_identifiers`` mapping per channel and a memory-entity link, all
    in one transaction, with ``user_accounts`` as the last of five. This route
    writes the ``user_accounts`` row and nothing else, so the account has no
    ``person_id`` and will not resolve through ``get_owner_person`` or the
    channel identity path. Saying "the same thing the CLI does" was wrong, and
    a false equivalence in a docstring is how the next person ships the gap
    rather than the rest of it.

    ``status`` depends on ``sso``, and the difference is load-bearing rather
    than cosmetic:

    * without ``sso`` the account is ``invited`` — a placeholder the operator
      finishes with ``genus user set-password``, and a row that
      ``jit_provision`` and ``issue_for_account`` both refuse outright;
    * with ``sso`` it is ``active`` with no password and no IdP binding, which
      is the shape ``bootstrap_owner_account`` already creates the owner in.
      Nothing can sign in as it: ``verify_password`` refuses a NULL hash, and
      an email match alone never binds an identity. The ONE way in is the
      one-shot grant armed below, which requires a verified claim for that
      address. An ``invited`` row could not consume that grant at all
      (``create_binding_grant`` refuses a non-active target), so "invited plus
      a grant" would be an invitation nobody could accept.
    """
    actor = require_operator(request)
    _, tenant_id, caller_role = _identity(request)
    email = _email(body.email)
    role = _role(body.role)
    # Before anything is written, and before the address is even looked up: an
    # invite is how a caller would mint the authority it could not grant by
    # PATCH, and an SSO invite at `owner` would come with a live binding grant
    # attached.
    _refuse_an_authority_change_the_caller_cannot_make(
        request, caller_role=caller_role, event="user.invite", action="-", new_role=role
    )
    display_name = (body.display_name or "").strip() or email.split("@", 1)[0]
    status = "active" if body.sso else "invited"

    try:
        created = accounts.create_account(
            tenant_id=tenant_id,
            email=email,
            display_name=display_name,
            role=role,
            status=status,
        )
    except psycopg2.errors.UniqueViolation as exc:
        # The owner index (migration 071) is a partial UNIQUE on tenant_id: an
        # appliance has one owner row, so a second invite at that role is a
        # conflict with the appliance's state rather than a bad request.
        audited(request, "user.invite", action="-", status="denied", role=role, reason="conflict")
        raise HTTPException(
            status_code=409, detail="that tenant already has an owner account"
        ) from exc
    except (psycopg2.Error, OSError) as exc:
        raise _unavailable(exc) from exc

    if created is None:
        audited(request, "user.invite", action="-", status="denied", role=role, reason="duplicate")
        raise HTTPException(status_code=409, detail="an account with that address already exists")

    payload: dict[str, Any] = {"user": _account_payload(created)}
    if body.sso:
        payload["grant"] = _grant_payload(_arm_grant(request, email, tenant_id, actor))

    audited(
        request,
        "user.invite",
        action=str(created["id"]),
        # Identifiers only: the id, the role, and whether a grant was armed.
        # The address is deliberately absent — see this module's header.
        role=role,
        account_status=status,
        sso=body.sso,
        grant_id=payload.get("grant", {}).get("id"),
    )
    return JSONResponse(content=payload, status_code=201)


@router.patch("/api/users/{user_id}")
def update_user(user_id: str, body: AccountPatch, request: Request) -> dict[str, Any]:
    """Change an account's role, name or status.

    Two refusals stand between this route and an appliance nobody can
    administer, and they are separate checks because they fail for different
    reasons — one is about the INSTANCE (somebody must be owner), the other
    about the CALLER (a session must not drop its own authority).
    """
    actor = require_operator(request)
    caller_id, tenant_id, caller_role = _identity(request)
    target_id = _account_id(user_id)
    row = _load(target_id, tenant_id)

    role = _role(body.role) if body.role is not None else None
    drops_authority = _drops_authority(role, body.status)

    _refuse_an_authority_change_the_caller_cannot_make(
        request,
        caller_role=caller_role,
        event="user.update",
        action=target_id,
        target_role=row.get("role"),
        new_role=role,
        new_status=body.status,
    )

    # The belt to the role check's braces, and it counts owner ROWS whatever
    # their status: an ``invited`` owner still holds migration 071's single
    # owner slot, so demoting it leaves the appliance with no owner and the
    # slot free. Keyed on ``status == "active"``, this guard did not fire at
    # all for the state ``genus user add --role owner`` actually writes.
    #
    # Before the self-check, not after it: the owner demoting themselves trips
    # both, and "this is the tenant's only owner" is the reason that survives
    # asking somebody else to do it.
    if drops_authority and row.get("role") == OWNER_ROLE:
        try:
            remaining = accounts.owner_count(tenant_id, excluding_id=target_id, active_only=False)
        except (psycopg2.Error, OSError) as exc:
            raise _unavailable(exc) from exc
        if remaining == 0:
            audited(request, "user.update", action=target_id, status="denied", reason="last_owner")
            raise HTTPException(
                status_code=409,
                detail=(
                    "this is the tenant's only owner — an instance with no owner "
                    "cannot be administered. Make somebody else an owner first, "
                    "or use `genus user` on the box."
                ),
            )

    if drops_authority and target_id == caller_id:
        audited(request, "user.update", action=target_id, status="denied", reason="self_demotion")
        raise HTTPException(
            status_code=409,
            detail=(
                "you cannot demote or disable your own account — the session you "
                "are holding keeps its current role until it expires. Ask another "
                "operator, or use `genus user` on the box."
            ),
        )

    try:
        updated = accounts.update_account(
            target_id,
            tenant_id=tenant_id,
            role=role,
            display_name=body.display_name,
            status=body.status,
        )
    except psycopg2.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail="that tenant already has an owner account"
        ) from exc
    except (psycopg2.Error, OSError) as exc:
        raise _unavailable(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="no account with that id")

    revoked = 0
    if body.status == "disabled":
        # Without this the disabled account keeps working for up to thirty
        # days: a refresh token IS the session, and nothing about the status
        # column reaches one that has already been minted.
        try:
            revoked = accounts.revoke_user_sessions(target_id)
        except (psycopg2.Error, OSError) as exc:
            # The row is already disabled and the sessions are not. That half
            # state is audited BEFORE the refusal rather than after it, because
            # a change that happened and was never recorded is the shape that
            # gets read afterwards as "this never happened" — and the answer
            # has to say which half is outstanding, since a plain retry of this
            # same PATCH is what fixes it.
            audited(
                request,
                "user.update",
                action=target_id,
                status="error",
                account_status="disabled",
                reason="sessions_not_revoked",
            )
            logger.warning("could not revoke sessions for a disabled account: %s", type(exc))
            raise HTTPException(
                status_code=503,
                detail=(
                    "the account is disabled, but its live sessions could not be "
                    "revoked — repeat this request once the account store is back"
                ),
            ) from exc

    audited(
        request,
        "user.update",
        action=target_id,
        role=role,
        # The role it REPLACED. Without it an owner→member demotion and a
        # member→member no-op are the same row in the SIEM, which is the wrong
        # side of the investigation this surface would be investigated for.
        previous_role=row.get("role"),
        account_status=body.status,
        renamed=body.display_name is not None,
        sessions_revoked=revoked or None,
        actor_hint=actor,
    )
    return {"user": _account_payload(updated)}


# ── SSO binding grants ──────────────────────────────────────────────────


@router.post("/api/users/{user_id}/binding-grant", status_code=201)
def arm_binding_grant(user_id: str, request: Request) -> JSONResponse:
    """Arm a one-shot grant so this account's next SSO sign-in binds it.

    The address comes from the ROW, never from the request: a grant is armed
    against an email, and letting the caller name it would turn this route into
    a way to arm a grant for an address that is not the account's.

    **Only an owner may arm one on the owner's account.** This is the second
    road to the owner slot and it stays open when the first is blocked:
    ``bootstrap_owner_account`` leaves the owner ``active`` with no password
    and no IdP binding, which is precisely the shape ``create_binding_grant``
    accepts — and consuming the grant binds the presenter's identity onto that
    account. The CLI equivalent is a shell on the box; this is an HTTP route.
    """
    actor = require_operator(request)
    _, tenant_id, caller_role = _identity(request)
    target_id = _account_id(user_id)
    row = _load(target_id, tenant_id)

    if row.get("role") == OWNER_ROLE and caller_role != OWNER_ROLE:
        audited(
            request,
            "user.binding_grant",
            action=target_id,
            status="denied",
            reason="owner_account",
        )
        raise HTTPException(
            status_code=403,
            detail="only an owner may arm a binding grant on the owner account",
        )

    grant = _arm_grant(request, accounts.canonical_email(row.get("email")), tenant_id, actor)
    audited(
        request,
        "user.binding_grant",
        action=target_id,
        grant_id=str(grant["id"]),
        ttl_seconds=BINDING_GRANT_TTL_SECONDS,
    )
    return JSONResponse(content={"grant": _grant_payload(grant)}, status_code=201)


@router.get("/api/users/{user_id}/binding-grants")
def list_user_binding_grants(user_id: str, request: Request) -> dict[str, Any]:
    """Every grant ever armed for this account, live or spent.

    ``list_binding_grants`` answers per TENANT, so the account's own address is
    the filter — applied here rather than by widening the DAL, because a
    per-email query is not a question anything else asks.
    """
    require_operator(request)
    _, tenant_id, _ = _identity(request)
    target_id = _account_id(user_id)
    row = _load(target_id, tenant_id)
    email = accounts.canonical_email(row.get("email"))

    try:
        grants = accounts.list_binding_grants(tenant_id, include_inactive=True)
    except (psycopg2.Error, OSError) as exc:
        raise _unavailable(exc) from exc
    mine = [grant for grant in grants if accounts.canonical_email(str(grant.get("email"))) == email]
    return {"grants": [_grant_listing_payload(grant) for grant in mine], "count": len(mine)}
