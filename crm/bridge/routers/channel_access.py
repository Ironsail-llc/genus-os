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

import re
from typing import Any

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from robothor.engine.channels import identities
from routers._audit import audited
from routers._operator import require_operator


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

_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


class ApproveRequest(BaseModel):
    """Who the code's sender is, in this instance's own terms.

    Exactly one of ``user_id`` and ``email`` — the DAL enforces that too, but a
    400 here is a better answer than a spent code and an exception.
    """

    user_id: str | None = None
    email: str | None = None
    role: str = "member"


def _channel(name: str) -> str:
    if not _CHANNEL_NAME.match(name or ""):
        raise HTTPException(status_code=400, detail="unrecognized channel name")
    return name


def _code(value: str) -> str:
    if not _CODE.match((value or "").strip().upper()):
        raise HTTPException(status_code=400, detail="not a pairing code")
    return value.strip().upper()


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _settled(exc: identities.PairingError) -> HTTPException:
    """Turn a DAL refusal into the status an operator can act on.

    ``PairingCodeError`` is a 404 and not a 400: from the operator's side the
    code they were given simply is not there any more, which is the same
    situation whether it expired, was spent or was denied — and saying WHICH
    would tell a caller holding a guessed code that it had once been real.
    """
    if isinstance(exc, identities.PairingCodeError):
        return HTTPException(status_code=404, detail="no live pairing code matched")
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/{name}/pending")
def list_pending(name: str, request: Request) -> dict[str, Any]:
    """Codes waiting on a decision. Never a code, never a native id."""
    require_operator(request)
    channel = _channel(name)
    rows = identities.list_pending(channel)
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
def approve(name: str, code: str, body: ApproveRequest, request: Request) -> dict[str, Any]:
    """Spend a code and bind its sender to a user."""
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
        raise HTTPException(status_code=400, detail="name exactly one of user_id or email")

    try:
        identity = identities.approve_pairing(
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

    audited(
        request,
        "channel.pairing.approve",
        action=channel,
        identity_id=str(identity["id"]),
        role=body.role,
    )
    return {"id": str(identity["id"]), "channel": channel, "role": body.role}


@router.post("/{name}/pairings/{code}/deny")
def deny(name: str, code: str, request: Request) -> dict[str, Any]:
    """Spend a code without binding anything.

    Denial goes through the same one-shot statement as approval rather than
    deleting the row, so a denied code can never be approved afterwards by
    somebody holding the same value.
    """
    actor = require_operator(request)
    channel = _channel(name)
    checked = _code(code)

    try:
        denied = identities.deny_pairing(checked, actor=actor, channel=channel)
    except identities.PairingError as exc:
        audited(
            request,
            "channel.pairing.deny",
            action=channel,
            status="denied",
            reason=type(exc).__name__,
        )
        raise _settled(exc) from exc

    audited(request, "channel.pairing.deny", action=channel, pairing_id=str(denied["id"]))
    return {"denied": True, "channel": channel}


@router.get("/{name}/identities")
def list_bound(name: str, request: Request) -> dict[str, Any]:
    """Every live binding on a channel, for the operator who has to review them.

    This one DOES carry native ids: an operator reviewing who can drive their
    instance cannot review a list of opaque ids, and unlike ``/pending`` these
    are people they already decided about.
    """
    require_operator(request)
    channel = _channel(name)
    rows = identities.list_identities(channel)
    return {
        "channel": channel,
        "identities": [
            {
                "id": str(row["id"]),
                "user_id": row["user_id"],
                "native_id": row["native_id"],
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
def revoke(name: str, identity_id: str, request: Request) -> dict[str, Any]:
    """Soft-delete a binding. The same native id may pair again afterwards."""
    actor = require_operator(request)
    channel = _channel(name)
    if not _UUID.match(identity_id or ""):
        raise HTTPException(status_code=400, detail="not an identity id")

    revoked = identities.revoke(identity_id, actor=actor)
    audited(
        request,
        "channel.identity.revoke",
        action=channel,
        status="ok" if revoked else "denied",
        identity_id=identity_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="no live identity with that id")
    return {"revoked": True, "channel": channel, "id": identity_id}
