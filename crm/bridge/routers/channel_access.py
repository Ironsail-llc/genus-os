"""Who may reach this instance over a channel, from the Helm.

A stranger who messages a ``pairing`` channel is answered with a six-character
code and nothing else. Two things can settle that code: this router, and
``genus channel access`` on the box. Nothing else — and specifically not
anything reachable from the channel the code was sent over, which is the whole
point of the design and is enforced in
:mod:`robothor.engine.channels.identities`, not here.

So every route's first statement is ``require_operator`` and every mutation
passes ``actor=f"operator:{...}"`` down to the DAL, which refuses any value
that does not carry that prefix. A route that forgot the gate would not quietly
become an open approval endpoint: the DAL would refuse it too. The two checks
are deliberately not one check.

What the reads may say is the other half. ``GET /pending`` is the request a
compromised operator session would most want — the code itself, or the native
id of everyone currently knocking — so it returns neither, only that somebody
is waiting and when their code dies. ``role`` is capped at the roles pairing may
grant, mirroring ``accounts.JIT_PROVISIONABLE_ROLES``: a flow whose first step
is "a stranger sent a message" never ends in an admin.

Every route is ``def``, not ``async def``. All the work is psycopg2, which
belongs in FastAPI's worker threadpool rather than on the event loop — see
``crm/bridge/tests/test_route_concurrency.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

import psycopg2
from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from robothor.engine.channels import identities
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_operator

logger = logging.getLogger(__name__)


def _require_primary_tenant(tenant_id: str = Depends(get_tenant_id)) -> None:
    """Channel access is appliance-global, so deny secondary tenants.

    Symmetric with ``agent_manifests``: one engine owns one set of channels,
    and a second tenant's operator approving a pairing on them would be
    granting access to somebody else's instance.
    """
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        raise HTTPException(
            status_code=403,
            detail="appliance administration not authorized for tenant",
        )


router = APIRouter(
    prefix="/api/channels",
    tags=["channel-access"],
    dependencies=[Depends(_require_primary_tenant)],
)

#: What may appear in a channel name. It reaches a SQL parameter, never a
#: format string, so this is about refusing nonsense early with a 4xx rather
#: than about injection — an unbounded name would also be an unbounded audit
#: ``action`` value.
_CHANNEL_NAME = re.compile(r"^[a-z0-9_]{1,32}$")

#: A pairing code's own alphabet, plus the length. Checked here so a mistyped
#: value is a 400 rather than a hash lookup that quietly matches nothing.
_CODE = re.compile(f"^[{identities.PAIRING_CODE_ALPHABET}]{{{identities.PAIRING_CODE_LENGTH}}}$")


class ApproveRequest(BaseModel):
    """Who the code's sender is, in this instance's own terms.

    Exactly one of ``user_id`` and ``email`` — the DAL enforces that too, but a
    400 here is a better answer than a spent code and an exception.
    """

    user_id: str | None = None
    email: str | None = None
    #: ``viewer`` and not ``member``: see ``identities.DEFAULT_PAIRED_ROLE``.
    #: ``member``'s seeded policy allows every tool and migration 088 narrows it
    #: only for the ``__default__`` tenant, so the previous default was a cap in
    #: name only. Naming ``member`` still works.
    role: str = identities.DEFAULT_PAIRED_ROLE


def _channel(name: str) -> str:
    if not _CHANNEL_NAME.match(name or ""):
        raise HTTPException(status_code=422, detail="unrecognized channel name")
    return name


def _code(value: str) -> str:
    if not _CODE.match((value or "").strip().upper()):
        raise HTTPException(status_code=422, detail="not a pairing code")
    return value.strip().upper()


def _identity_id(value: str) -> str:
    """An identity id, or 422.

    ``uuid.UUID`` and not a regex. The regex this replaced was
    ``[0-9a-fA-F-]{36}``, which matches thirty-six hyphens, and equally
    thirty-six letter ``a`` characters -- both of which reached
    ``WHERE id = %s`` on a UUID column and came back as a 500. A caller's typo
    is not an application crash, and a shape check that admits values the column
    cannot hold is not a shape check.
    """
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="not an identity id") from None
    return str(value)


async def _tell_the_engine_to_forget() -> None:
    """Drop the engine's identity caches, best effort.

    The row is written in THIS process. The engine's belief about who a sender
    is lives in its own -- 60s in ``identity.resolvers``, **300s** in
    ``engine.users`` -- so without this an operator watches a revoke succeed and
    the revoked sender goes on driving the agent for up to five minutes, with
    nothing anywhere saying why.

    Best effort on purpose: the decision is already durable and the caches
    expire by themselves, so failing the operator's approval because the engine
    is mid-restart would be a worse outcome than the staleness this shortens.
    The failure is logged, never raised.
    """
    try:
        status, _ = await engine_request("POST", "/api/admin/identities/reload")
    except Exception:  # noqa: BLE001 - a settled decision must not fail on this
        logger.warning("Could not reach the engine to drop its identity caches", exc_info=True)
        return
    if status >= 400:
        logger.warning("Engine refused an identity cache reload (status %s)", status)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _settled(exc: identities.PairingError) -> HTTPException:
    """Turn a DAL refusal into the status an operator can act on.

    ``PairingCodeError`` is a 404 and not a 400: from the operator's side the
    code they were given simply is not there any more, which is the same
    situation whether it expired, was spent or was denied — and saying WHICH
    would tell a caller holding a guessed code that it had once been real.

    ``PairingConflictError`` is a 409, and its message DOES travel: unlike the
    code errors it says nothing about a credential, and "that user already has a
    Telegram binding" is the whole of what the operator needs in order to fix
    it. It used to be an unhandled ``UniqueViolation``, and therefore a 500.
    """
    if isinstance(exc, identities.PairingCodeError):
        return HTTPException(status_code=404, detail="no live pairing code matched")
    if isinstance(exc, identities.PairingConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _unavailable(exc: psycopg2.Error) -> HTTPException:
    """A database that is not answering is a dependency being down.

    503 and not 500: an operator paged at 3am needs to know whether the
    appliance crashed or Postgres did, and every route here is one query deep.
    """
    logger.warning("channel access route could not reach the database: %s", type(exc).__name__)
    return HTTPException(status_code=503, detail="the identity store is unavailable")


@router.get("/{name}/pending")
def list_pending(name: str, request: Request) -> dict[str, Any]:
    """Codes waiting on a decision. Never a code, never a native id."""
    require_operator(request)
    channel = _channel(name)
    try:
        rows = identities.list_pending(channel)
    except psycopg2.Error as exc:
        raise _unavailable(exc) from exc
    return {
        "channel": channel,
        "pending": [
            {
                "id": str(row["id"]),
                "channel": row["channel"],
                "expires_at": _iso(row["expires_at"]),
                "created_at": _iso(row.get("created_at")),
                "display_name_present": bool(row.get("display_name_present")),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.post("/{name}/pairings/{code}/approve")
async def approve(name: str, code: str, body: ApproveRequest, request: Request) -> dict[str, Any]:
    """Spend a code and bind its sender to a user.

    ``async`` because it awaits the engine after the write; the psycopg2 work
    goes through ``asyncio.to_thread``, so nothing blocking runs on the loop.
    """
    actor = require_operator(request)
    channel = _channel(name)
    checked = _code(code)

    if body.role not in identities.PAIRABLE_ROLES:
        audited(
            request,
            "channel.pairing.approve",
            action=channel,
            status="denied",
            role=body.role,
            reason="role_not_pairable",
        )
        raise HTTPException(status_code=400, detail=f"role {body.role!r} is not one pairing grants")
    if bool(body.user_id) == bool(body.email):
        # Audited like its sibling above. One of two refusal arms writing a row
        # is the shape that gets read afterwards as "this never happened".
        audited(
            request,
            "channel.pairing.approve",
            action=channel,
            status="denied",
            role=body.role,
            reason="no_single_target",
        )
        raise HTTPException(status_code=400, detail="name exactly one of user_id or email")

    try:
        identity = await asyncio.to_thread(
            identities.approve_pairing,
            checked,
            actor=actor,
            channel=channel,
            user_id=body.user_id,
            email=body.email,
            role=body.role,
        )
    except identities.PairingError as exc:
        audited(
            request,
            "channel.pairing.approve",
            action=channel,
            status="denied",
            role=body.role,
            reason=type(exc).__name__,
        )
        raise _settled(exc) from exc
    except psycopg2.Error as exc:
        raise _unavailable(exc) from exc

    audited(
        request,
        "channel.pairing.approve",
        action=channel,
        identity_id=str(identity["id"]),
        role=body.role,
    )
    await _tell_the_engine_to_forget()
    return {"id": str(identity["id"]), "channel": channel, "role": body.role}


@router.post("/{name}/pairings/{code}/deny")
async def deny(name: str, code: str, request: Request) -> dict[str, Any]:
    """Spend a code without binding anything.

    Denial goes through the same one-shot statement as approval rather than
    deleting the row, so a denied code can never be approved afterwards by
    somebody holding the same value.
    """
    actor = require_operator(request)
    channel = _channel(name)
    checked = _code(code)

    try:
        denied = await asyncio.to_thread(
            identities.deny_pairing, checked, actor=actor, channel=channel
        )
    except identities.PairingError as exc:
        audited(
            request,
            "channel.pairing.deny",
            action=channel,
            status="denied",
            reason=type(exc).__name__,
        )
        raise _settled(exc) from exc
    except psycopg2.Error as exc:
        raise _unavailable(exc) from exc

    audited(request, "channel.pairing.deny", action=channel, pairing_id=str(denied["id"]))
    await _tell_the_engine_to_forget()
    return {"denied": True, "channel": channel}


@router.get("/{name}/identities")
def list_bound(name: str, request: Request) -> dict[str, Any]:
    """Every live binding on a channel, for the operator who has to review them.

    The native id is FINGERPRINTED, not returned. Telling two bindings apart is
    what an operator needs; reading a workspace's member ids is not, and one
    leaked operator session would otherwise enumerate every bound account on the
    channel in a single call. ``user_id`` and ``display_name`` stay: they are
    this instance's own names for its own people, which is a different thing
    from a third-party platform's identifier for them.
    """
    require_operator(request)
    channel = _channel(name)
    try:
        rows = identities.list_identities(channel)
    except psycopg2.Error as exc:
        raise _unavailable(exc) from exc
    return {
        "channel": channel,
        "identities": [
            {
                "id": str(row["id"]),
                "user_id": row["user_id"],
                "native_id_fingerprint": identities.fingerprint(row["native_id"]),
                "display_name": row["display_name"],
                "role": row["role"],
                "paired_at": _iso(row["paired_at"]),
                "paired_by": row["paired_by"],
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.delete("/{name}/identities/{identity_id}")
async def revoke(name: str, identity_id: str, request: Request) -> dict[str, Any]:
    """Soft-delete a binding. The same native id may pair again afterwards.

    This is the only control an operator has when a binding goes bad, which is
    why it awaits the engine cache drop rather than leaving the revoked sender
    resolvable for another 300 seconds.
    """
    actor = require_operator(request)
    channel = _channel(name)
    checked = _identity_id(identity_id)

    try:
        revoked = await asyncio.to_thread(identities.revoke, checked, actor=actor)
    except psycopg2.Error as exc:
        raise _unavailable(exc) from exc
    audited(
        request,
        "channel.identity.revoke",
        action=channel,
        status="ok" if revoked else "denied",
        identity_id=checked,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="no live identity with that id")
    await _tell_the_engine_to_forget()
    return {"revoked": True, "channel": channel, "id": checked}
