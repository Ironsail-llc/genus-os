"""Remote tools: a parent tenant's agent running a tool a descendant's engine has.

Connectors are installed per company: a child's plugins live in the child's
engine, never in the parent's. These routes are the one path between them --
home engine -> bridge -> target engine; engines never call each other.

``GET /api/org/connections`` (a person: a parent's owner or admin at home,
the gate ``routers.org`` uses) asks every descendant's engine what it offers
(``GET /api/admin/remote-tools``). A company that does not answer is that
entry's ``status: "error"``; the others still answer.

``GET /api/org/remote/catalogue`` and ``POST /api/org/remote/call`` answer an
ENGINE: a service token carrying ``org:remote`` (``middleware`` refuses
anything else before these run). The token's tenant is the HOME tenant; the
body never names it. Before anything travels the bridge checks, in order:

1. the body's shape;
2. that the target is a descendant of home -- anything else is a 404 with the
   engine's own unreachable sentence, so an unknown tenant and a forbidden one
   look the same;
3. who the call is for: a named person must act in the target
   (``acting_role``), as its ``owner`` for a write; no person means an
   unattended read, allowed only while home grants its agents child data
   access, and never a write.

It then forwards with ``engine_request`` (one attempt; ``POST`` is never
retried) adding ``home_tenant`` from the token and the BRIDGE's own
attestations, and returns the target's status and body unchanged. Every call,
refused or forwarded, is audited, filed under the target once one is known.
Arguments are audited as a digest, never as values.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from robothor.tenancy import remote_contract as rc
from robothor.tenancy.remote_contract import (
    ACCESS_VALUES,
    ACCESS_WRITE,
    BRIDGE_TIMEOUT_S,
    ERR_BAD_ARGUMENTS,
    ERR_NOT_PERMITTED,
    ERR_TIMEOUT,
    ERR_WRITE_FORBIDDEN,
    SCOPE_ORG_REMOTE,
    args_digest,
    is_clean_refusal,
    is_known_answer,
)
from routers import _engine_client
from routers._audit import audited
from routers.org import _require_home_operator

router = APIRouter(prefix="/api/org", tags=["org"])

#: Each company's answer on the connections page. The page waits for this
#: route on first load, so a slow or dead engine is an error entry after this
#: long, never a held response. (The 165 s budget is for a CALL only.)
CONNECTIONS_TIMEOUT_S = rc.CONNECTIONS_TIMEOUT_S

_LIST_PATH = "/api/admin/remote-tools"
_CALL_PATH = "/api/admin/remote-tools/call"

#: A tool name as a path segment: what plugin tool names are, and nothing that
#: could move the request (``_engine_client._checked_path`` is the sink check).
_TOOL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_MAX_TENANT_ID = 128


class _RefusedError(Exception):
    """A refusal in the contract's shape: ``{"error": code, "message": ...}``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"error": self.code, "message": self.message, "attestation": None},
        )


def _require_engine(request: Request) -> Any:
    """The caller: an engine's service token with ``org:remote``.

    ``middleware`` refuses everyone else first; this is the route's own check,
    so a change to the middleware cannot open the route by itself.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not auth.is_service or not auth.has_scope(SCOPE_ORG_REMOTE):
        raise _RefusedError(403, ERR_NOT_PERMITTED, "An engine token with org:remote is required.")
    return auth


async def _check_target(home: str, raw: Any) -> str:
    """``raw`` as a descendant of ``home``, or the engine's unreachable refusal."""
    from robothor.engine.permissions import resolve_accessible_tenants
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    target = raw.strip() if isinstance(raw, str) else ""
    if target and len(target) <= _MAX_TENANT_ID and target != home:
        reach = await asyncio.to_thread(resolve_accessible_tenants, home, "owner")
        if target in reach:
            return target
    raise _RefusedError(404, ERR_NOT_PERMITTED, UNREACHABLE)


async def _check_requester(home: str, target: str, requested_by: str | None, access: str) -> None:
    if requested_by:
        from robothor.auth.acting import acting_role

        role = await asyncio.to_thread(acting_role, requested_by, home, target)
        if role is None:
            raise _RefusedError(
                403, ERR_NOT_PERMITTED, "The person this call is for may not act in that company."
            )
        if access == ACCESS_WRITE and role != "owner":
            raise _RefusedError(
                403, ERR_WRITE_FORBIDDEN, "Only an owner of the parent may change another company."
            )
        return
    if access == ACCESS_WRITE:
        raise _RefusedError(
            403, ERR_WRITE_FORBIDDEN, "An unattended run never changes another company."
        )
    from robothor.crm.dal import tenant_child_data_access

    if not await asyncio.to_thread(tenant_child_data_access, home):
        raise _RefusedError(
            403,
            ERR_NOT_PERMITTED,
            "This company has not granted its agents access to its child companies.",
        )


def _bad(message: str) -> _RefusedError:
    return _RefusedError(422, ERR_BAD_ARGUMENTS, message)


def _parse_call(body: Any) -> dict[str, Any]:
    """The call body's fields, typed; anything malformed is a 422."""
    if not isinstance(body, dict):
        raise _bad("The body must be a JSON object.")
    tool = body.get("tool")
    if not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool):
        raise _bad("`tool` must be a tool name.")
    args = body.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise _bad("`args` must be an object.")
    access = body.get("access")
    if access not in ACCESS_VALUES:
        raise _bad("`access` must be read or write.")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise _bad("`dry_run` must be true or false.")
    request_id = body.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 200):
        raise _bad("`request_id` must be a string.")
    if access == ACCESS_WRITE and not request_id:
        raise _bad("A write needs a `request_id`.")
    requested_by = body.get("requested_by")
    if requested_by is not None and (not isinstance(requested_by, str) or len(requested_by) > 200):
        raise _bad("`requested_by` must be a user id or null.")
    return {
        "tool": tool,
        "args": args,
        "access": access,
        "dry_run": dry_run,
        # Only a write is deduplicated in the target; a read carries no id.
        "request_id": (request_id or None) if access == ACCESS_WRITE else None,
        "requested_by": requested_by or None,
    }


def _attestations() -> list[str]:
    from robothor.auth.runtime import org_remote_attestations

    return sorted(org_remote_attestations())


@router.post("/remote/call")
async def remote_call(request: Request) -> JSONResponse:
    """Run one declared tool in a descendant's engine, for the calling engine."""
    target: str | None = None
    call: dict[str, Any] = {}
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        auth = _require_engine(request)
        home = auth.tenant_id
        call = _parse_call(body)
        target = await _check_target(home, body.get("tenant"))
        await _check_requester(home, target, call["requested_by"], call["access"])
    except _RefusedError as refused:
        _audit_call(request, target, call, body, status="denied", reason=refused.code)
        return refused.response()

    forwarded = {
        **call,
        "home_tenant": home,
        "agent_id": auth.agent_id or auth.user_id,
        "attestations": _attestations(),
    }
    status, answer = await _engine_client.engine_request(
        "POST", _CALL_PATH, json=forwarded, timeout=BRIDGE_TIMEOUT_S, tenant=target
    )
    outcome = answer.get("outcome") if isinstance(answer, dict) else None
    _audit_call(
        request,
        target,
        forwarded,
        body,
        status=_call_status(call["access"], status, answer),
        reason=str(answer.get("error") or "") if isinstance(answer, dict) else "",
        http_status=status,
        outcome=outcome,
    )
    return JSONResponse(status_code=status, content=answer)


def _call_status(access: str, status: int, answer: Any) -> str:
    """How a forwarded call reads in the audit: for a write, anything but a
    known answer or a refusal given before it ran is ``unknown``."""
    if isinstance(answer, dict) and answer.get("outcome_unknown") is True:
        return "unknown"
    if status == 200 and is_known_answer(answer):
        return "ok"
    if is_clean_refusal(status, answer):
        return "denied"
    if access == ACCESS_WRITE:
        return "unknown"
    return "denied" if status in (403, 404, 409, 413, 422) else "error"


def _audit_call(
    request: Request,
    target: str | None,
    call: dict[str, Any],
    body: Any,
    *,
    status: str,
    reason: str = "",
    **extra: Any,
) -> None:
    auth = getattr(request.state, "auth", None)
    raw = body if isinstance(body, dict) else {}
    args = call.get("args", raw.get("args"))
    audited(
        request,
        "org.remote_call",
        action=str(call.get("tool") or raw.get("tool") or "")[:128],
        status=status,
        audit_tenant_id=target,
        home_tenant_id=getattr(auth, "tenant_id", None),
        target_tenant_id=target,
        user_id=call.get("requested_by"),
        agent_id=getattr(auth, "agent_id", None),
        access=call.get("access"),
        dry_run=call.get("dry_run"),
        request_id=call.get("request_id"),
        args_digest=args_digest(args) if isinstance(args, dict) else None,
        reason=reason or None,
        **extra,
    )


@router.get("/remote/catalogue")
async def remote_catalogue(
    request: Request, tenant: str = "", tool: str | None = None
) -> JSONResponse:
    """A descendant's remote tools, or one tool's full description."""
    target: str | None = None
    try:
        auth = _require_engine(request)
        if tool is not None and not _TOOL_NAME.fullmatch(tool):
            raise _bad("`tool` must be a tool name.")
        target = await _check_target(auth.tenant_id, tenant)
    except _RefusedError as refused:
        audited(
            request,
            "org.remote_catalogue",
            action=str(tool or "")[:128] or "list",
            status="denied",
            audit_tenant_id=target,
            reason=refused.code,
        )
        return refused.response()
    path = f"{_LIST_PATH}/{tool}" if tool else _LIST_PATH
    status, answer = await _engine_client.engine_request("GET", path, tenant=target)
    audited(
        request,
        "org.remote_catalogue",
        action=tool or "list",
        status="ok" if status == 200 else "error",
        audit_tenant_id=target,
        home_tenant_id=auth.tenant_id,
        target_tenant_id=target,
        http_status=status,
    )
    return JSONResponse(status_code=status, content=answer)


def _entry(target: str, name: str, status: int, body: Any) -> dict[str, Any]:
    if status == 200 and isinstance(body, dict):
        return {
            "tenant_id": target,
            "display_name": name,
            "status": "ok",
            "error": None,
            "plugins": body.get("plugins") or [],
            "tools": body.get("tools") or [],
            "checks": body.get("checks") or [],
        }
    error = ""
    if isinstance(body, dict):
        error = str(body.get("message") or body.get("error") or "")
    return {
        "tenant_id": target,
        "display_name": name,
        "status": "error",
        "error": error or f"HTTP {status}",
        "plugins": [],
        "tools": [],
        "checks": [],
    }


def _display_name(tenant_id: str) -> str:
    from robothor.crm.dal import get_tenant

    try:
        row = get_tenant(tenant_id) or {}
    except Exception:  # noqa: BLE001 - a name is decoration; the entry still answers
        return tenant_id
    return str(row.get("displayName") or tenant_id)


async def _list_tools(target: str) -> tuple[int, Any]:
    """One company's catalogue, or a 504 once :data:`CONNECTIONS_TIMEOUT_S` passes."""
    try:
        return await asyncio.wait_for(
            _engine_client.engine_request(
                "GET", _LIST_PATH, tenant=target, timeout=CONNECTIONS_TIMEOUT_S
            ),
            # The client's own timeout bounds each phase; this bounds the whole.
            timeout=CONNECTIONS_TIMEOUT_S + 1,
        )
    except TimeoutError:
        return 504, {"error": ERR_TIMEOUT, "message": "That company's engine did not answer."}


@router.get("/connections")
async def org_connections(request: Request) -> dict[str, Any]:
    """What each descendant's engine offers its parent: plugins, tools, checks."""
    from robothor.engine.permissions import resolve_accessible_tenants

    auth = _require_home_operator(request)
    home = auth.home_tenant_id
    reach = await asyncio.to_thread(resolve_accessible_tenants, home, auth.role)
    targets = [t for t in reach if t != home]
    names = await asyncio.to_thread(lambda: {t: _display_name(t) for t in targets})
    answers = list(await asyncio.gather(*(_list_tools(t) for t in targets)))
    tenants = [
        _entry(t, names[t], status, body)
        for t, (status, body) in zip(targets, answers, strict=True)
    ]
    for entry in tenants:
        audited(
            request,
            "org.connections",
            action=entry["tenant_id"],
            status="ok" if entry["status"] == "ok" else "error",
            audit_tenant_id=entry["tenant_id"],
            user_id=auth.user_id,
            home_tenant_id=home,
            target_tenant_id=entry["tenant_id"],
        )
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "home_tenant_id": home,
        "tenants": tenants,
    }
