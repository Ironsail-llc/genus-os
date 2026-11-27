"""``org_tools``, ``org_tool_read``, ``org_tool_write``: a child company's OWN tools.

Connectors are installed per company, so a parent's engine does not have its
children's plugins. These tools reach them through the bridge (``GET
/api/org/remote/catalogue``, ``POST /api/org/remote/call``), which forwards to
the child's engine; the child runs the tool under its own RBAC and audit.
Nothing here enters the child's database scope.

They take ``tenant`` in their own schema, so the engine's generic ``tenant``
handling (``cross_tenant.admit``) passes them, and :func:`_admit` decides
instead, with the same helpers and in this order:

1. the mode (``GENUS_ORG_REMOTE_TOOLS``): ``off`` refuses all three, ``read``
   refuses the write;
2. the shape of ``tenant`` (a malformed one reads as unreachable);
3. a live, top-level, non-benchmark run;
4. a tenant this run may reach (``ctx.accessible_tenant_ids``) that is not its
   own -- an unknown and a forbidden tenant get the same sentence;
5. a write: an attended run by the verified owner. An unattended run never
   writes;
6. ``dry_run``: a real boolean.

The bridge and the child check again; nothing here is trusted beyond this
engine. Every outcome writes one ``agent.cross_tenant_call`` row under the
home tenant, ``details.remote_tool`` naming the child's tool. A write is
journalled in the home effect ledger like any other (``effect_dispatch``) and
its effect id is the ``request_id`` the child deduplicates on; a replay is
answered from the ledger and never sent again.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from robothor.tenancy.remote_contract import (
    ACCESS_READ,
    ACCESS_WRITE,
    ERR_OUTCOME_UNKNOWN,
    ERR_TIMEOUT,
    ERR_TOOLS_OFF,
    ERR_WRITE_FORBIDDEN,
    HOME_TIMEOUT_S,
    MODE_DRY_RUN,
    MODE_OFF,
    MODE_READ,
    OUTCOME_DRY_RUN,
    OUTCOME_ERROR,
    OUTCOME_REPLAYED,
    SCOPE_ORG_REMOTE,
)

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

CATALOGUE_TOOL = "org_tools"
READ_TOOL = "org_tool_read"
WRITE_TOOL = "org_tool_write"

_CALL_PATH = "/api/org/remote/call"
_CATALOGUE_PATH = "/api/org/remote/catalogue"
_BRIDGE_SCOPES = ("bridge:read", "bridge:write", SCOPE_ORG_REMOTE)

#: A child's tool name, as the bridge accepts one.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

#: Said when a run reaches no child company, instead of an empty list.
NO_REACH_NOTE = (
    "This run reaches no other company. Child companies are reachable only from a "
    "top-level run by an owner or admin of the parent, or by an unattended agent the "
    "parent has granted child access to."
)

#: Target answers that mean the write may or may not have happened.
_UNKNOWN_CODES = frozenset({ERR_OUTCOME_UNKNOWN, ERR_TIMEOUT})


class _RefusalError(Exception):
    def __init__(self, result: dict[str, Any], target: str) -> None:
        super().__init__(result.get("error"))
        self.result, self.target = result, target


def _remote_name(args: dict[str, Any]) -> str | None:
    tool = args.get("tool")
    return tool[:128] if isinstance(tool, str) and tool else None


def _audit(
    name: str,
    ctx: ToolContext,
    target: str,
    outcome: str,
    remote_tool: str | None,
    *,
    dry_run: bool = False,
    reason: str = "",
) -> None:
    from robothor.engine.tools.cross_tenant import audit_cross_tenant

    audit_cross_tenant(
        home_tenant=ctx.tenant_id,
        target_tenant=target,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        tool=name,
        outcome=outcome,
        dry_run=dry_run,
        reason=reason,
        remote_tool=remote_tool,
    )


def _refuse(
    target: str, message: str, *, gate: str, gate_name: str, unlock: str, code: str | None = None
) -> _RefusalError:
    from robothor.engine.refusal import refusal

    extra = {"error_code": code} if code else {}
    return _RefusalError(
        refusal(message, gate=gate, name=gate_name, unlock=unlock, **extra), target
    )


def _session_run(ctx: ToolContext) -> Any:
    from robothor.engine.session_registry import lookup

    session = lookup(ctx.run_id) if ctx.run_id else None
    return getattr(session, "run", None)


def _attended(ctx: ToolContext) -> bool:
    from robothor.engine.run_identity import is_attended

    run = _session_run(ctx)
    return run is not None and is_attended(run, ctx.identity)


def _requested_by(ctx: ToolContext) -> str | None:
    """The person the call is for: the verified account of an attended run, else None."""
    if not _attended(ctx):
        return None
    identity = ctx.identity
    account = getattr(identity, "user_account_id", None)
    if not account and getattr(identity, "channel", "") == "webchat":
        # A webchat identity's identifier IS the account id.
        account = getattr(identity, "identifier", None)
    return str(account) if account else None


def _admit(name: str, args: dict[str, Any], ctx: ToolContext, access: str, mode: str) -> str:
    """The target tenant ("" for the catalogue of every company), or raise a refusal."""
    from robothor.engine.tools.cross_tenant import (
        MAX_TENANT_ID,
        UNREACHABLE,
        _tenant_target,
        is_top_level_run,
    )

    raw = args.get("tenant")
    shown = str(raw)[:MAX_TENANT_ID] if raw is not None else ""
    # 1. The mode.
    if mode == MODE_OFF:
        raise _refuse(
            shown,
            "Remote tools are turned off on this deployment.",
            gate="flag",
            gate_name="GENUS_ORG_REMOTE_TOOLS",
            unlock="An operator sets GENUS_ORG_REMOTE_TOOLS to read, dry_run or full.",
            code=ERR_TOOLS_OFF,
        )
    if access == ACCESS_WRITE and mode == MODE_READ:
        raise _refuse(
            shown,
            "Remote tools are read-only on this deployment; nothing was changed.",
            gate="flag",
            gate_name="GENUS_ORG_REMOTE_TOOLS",
            unlock="An operator sets GENUS_ORG_REMOTE_TOOLS to dry_run or full.",
            code=ERR_WRITE_FORBIDDEN,
        )
    # 2. The shape of the target.
    unreachable = {
        "gate": "role",
        "gate_name": "cross_tenant_reach",
        "unlock": "Call org_tools to see the companies this run can reach and their tools.",
    }
    target = _tenant_target(raw)
    if target is None:
        raise _refuse(shown, UNREACHABLE, **unreachable)
    if not target and name != CATALOGUE_TOOL:
        raise _refuse(
            "",
            f"'{name}' needs `tenant`: the id of the child company whose tool to run.",
            gate="policy",
            gate_name="remote_tool_arguments",
            unlock="Call org_tools to list the companies and their tools, then pass `tenant`.",
        )
    tool = args.get("tool")
    if (name != CATALOGUE_TOOL or tool is not None) and (
        not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool)
    ):
        raise _refuse(
            target,
            "`tool` must be the name of one of that company's tools.",
            gate="policy",
            gate_name="remote_tool_arguments",
            unlock="Call org_tools with `tenant` to list that company's tools.",
        )
    if "args" in args and args["args"] is not None and not isinstance(args["args"], dict):
        raise _refuse(
            target,
            "`args` must be an object of that tool's arguments.",
            gate="policy",
            gate_name="remote_tool_arguments",
            unlock="Call org_tools with `tenant` and `tool` to see its parameters.",
        )
    # 3. A top-level run.
    if not is_top_level_run(ctx):
        raise _refuse(
            target,
            "Only a top-level run can use another company's tools; sub-agent and "
            "benchmark runs cannot.",
            gate="policy",
            gate_name="cross_tenant_top_level",
            unlock="Report back and let the top-level run make this call.",
        )
    # 4. A reachable company other than this one.
    if target and target == ctx.tenant_id:
        raise _refuse(
            target,
            "That is your own company: call its tool directly, not through org_tool_*.",
            gate="policy",
            gate_name="remote_tool_own_tenant",
            unlock="Call the tool by its own name, without `tenant`.",
        )
    if target and target not in ctx.accessible_tenant_ids:
        raise _refuse(target, UNREACHABLE, **unreachable)
    # 5. A write: the verified owner, in person.
    if access == ACCESS_WRITE:
        from robothor.engine.owner_gate import is_verified_owner

        if not _attended(ctx):
            raise _refuse(
                target,
                "An unattended run never changes another company; nothing was changed.",
                gate="policy",
                gate_name="remote_write_unattended",
                unlock="Create a task for the owner to make this change in chat.",
                code=ERR_WRITE_FORBIDDEN,
            )
        if not is_verified_owner(ctx):
            raise _refuse(
                target,
                f"Only the verified owner may use '{name}'; nothing was changed.",
                gate="role",
                gate_name="cross_tenant_owner",
                unlock=(
                    "Create a task in that company for its team instead (create_task with "
                    "`tenant`), or ask the owner to make this change."
                ),
                code=ERR_WRITE_FORBIDDEN,
            )
        # 6. A real boolean.
        if "dry_run" in args and not isinstance(args["dry_run"], bool):
            from robothor.engine.tools.cross_tenant import _bad_dry_run

            raise _RefusalError(_bad_dry_run(name), target)
    return target


def _bridge_url(path: str) -> str:
    """The bridge as this deployment reaches it (``BRIDGE_URL`` when set)."""
    from robothor.config import get_config
    from robothor.services import get_service_url

    return get_service_url("bridge", path) or f"{get_config().bridge_url}{path}"


def _headers(ctx: ToolContext) -> dict[str, str]:
    from robothor.engine.tools.service_client import bridge_headers

    return bridge_headers(
        f"engine:{ctx.agent_id}",
        ctx.tenant_id,
        scopes=_BRIDGE_SCOPES,
        agent_id=ctx.agent_id,
    )


async def _send(
    method: str,
    path: str,
    ctx: ToolContext,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """One request to the bridge: ``(status, body)``, or ``(0, exc)`` when none came back.

    One attempt: a write may already have been acted on when a connection drops.
    """
    from robothor.http_clients import shared_async_client

    headers = await asyncio.to_thread(_headers, ctx)
    client = shared_async_client("service_client")
    try:
        response = await client.request(
            method,
            _bridge_url(path),
            params=params,
            json=body,
            headers=headers,
            timeout=HOME_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        logger.warning("remote tool call to the bridge failed (%s)", type(exc).__name__)
        return 0, exc
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"error": "the bridge returned an unreadable response"}


def _failure(status: int, answer: Any, *, write: bool) -> tuple[dict[str, Any], str]:
    """A non-200 (or no) answer as a tool result, and the audit outcome for it."""
    if status == 0:
        exc = answer
        sent_nothing = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
        result: dict[str, Any] = {
            "error": (
                "The bridge could not be reached; nothing was sent."
                if sent_nothing
                else f"The call to the bridge did not complete ({type(exc).__name__})."
            ),
            "retryable": sent_nothing or not write,
        }
        if write and not sent_nothing:
            result["outcome_unknown"] = True
        return result, "error"
    body = answer if isinstance(answer, dict) else {}
    if "message" not in body and status in (401, 403):
        # The bridge's own gate (auth, scope, capabilities manifest): its
        # sentence says what to fix, and it is no remote refusal code.
        reason = str(body.get("error") or f"HTTP {status}")
        return {
            "error": f"The bridge refused the call: {reason}",
            "error_code": None,
            "remote_status": status,
            "retryable": False,
        }, "denied"
    code = str(body.get("error") or "")
    message = str(body.get("message") or code or f"HTTP {status}")
    result = {
        "error": f"The company's engine refused the call: {message}"
        if status in (403, 404, 409, 413, 422)
        else f"The remote call failed: {message}",
        "error_code": code or None,
        "remote_status": status,
    }
    if body.get("attestation"):
        result["attestation"] = body["attestation"]
    if write and (code in _UNKNOWN_CODES or status in (502, 504)):
        result["outcome_unknown"] = True
        result["retryable"] = False
    refused = status in (403, 404, 409, 413, 422) and code not in _UNKNOWN_CODES
    return result, "denied" if refused else "error"


def _request_id() -> str:
    """The home effect ledger's id for this write; a fresh one outside the ledger."""
    from robothor.engine.runtime import effects

    record = effects.active_effect.get()
    if record and record.get("id"):
        return str(record["id"])
    return uuid.uuid4().hex


async def _mode() -> str:
    """The mode, read off the loop: the flag store may read the database."""
    from robothor.auth.runtime import org_remote_tools_mode

    return await asyncio.to_thread(org_remote_tools_mode)


async def _call(name: str, args: dict[str, Any], ctx: ToolContext, access: str) -> dict[str, Any]:
    remote = _remote_name(args)
    mode = await _mode()
    try:
        target = _admit(name, args, ctx, access, mode)
    except _RefusalError as refused:
        _audit(name, ctx, refused.target, "denied", remote, reason=str(refused.result["error"]))
        return refused.result

    dry_run = access == ACCESS_WRITE and (args.get("dry_run") is True or mode == MODE_DRY_RUN)
    body = {
        "tenant": target,
        "tool": args["tool"],
        "args": args.get("args") or {},
        "access": access,
        "dry_run": dry_run,
        "request_id": _request_id() if access == ACCESS_WRITE else None,
        "requested_by": _requested_by(ctx),
        "agent_id": ctx.agent_id,
    }
    status, answer = await _send("POST", _CALL_PATH, ctx, body=body)
    if status != 200 or not isinstance(answer, dict):
        result, outcome = _failure(status, answer, write=access == ACCESS_WRITE)
        _audit(name, ctx, target, outcome, remote, dry_run=dry_run, reason=_reason(result))
        return {"tenant": target, "tool": args["tool"], **result}

    outcome = str(answer.get("outcome") or "")
    result = {
        "tenant": target,
        "tool": args["tool"],
        "outcome": outcome,
        "result": answer.get("result"),
    }
    if answer.get("would") is not None:
        result["would"] = answer["would"]
    if outcome == OUTCOME_ERROR:
        inner = answer.get("result")
        detail = inner.get("error") if isinstance(inner, dict) else None
        result["error"] = f"'{args['tool']}' reported an error: {detail or 'see result'}"
    audit_outcome = {OUTCOME_ERROR: "error", OUTCOME_REPLAYED: "replay"}.get(outcome, "ok")
    _audit(
        name,
        ctx,
        target,
        audit_outcome,
        remote,
        dry_run=outcome == OUTCOME_DRY_RUN,
        reason=str(result.get("error") or ""),
    )
    return result


def _reason(result: dict[str, Any]) -> str:
    code = result.get("error_code")
    return f"{code}: {result['error']}" if code else str(result["error"])


async def _org_tool_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call(READ_TOOL, args, ctx, ACCESS_READ)


async def _org_tool_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call(WRITE_TOOL, args, ctx, ACCESS_WRITE)


async def _catalogue_of(
    target: str, tool: str | None, ctx: ToolContext, remote: str | None
) -> dict[str, Any]:
    params = {"tenant": target, **({"tool": tool} if tool else {})}
    status, answer = await _send("GET", _CATALOGUE_PATH, ctx, params=params)
    if status == 200 and isinstance(answer, dict):
        _audit(CATALOGUE_TOOL, ctx, target, "ok", remote)
        return {"tenant_id": target, "status": "ok", **answer}
    result, outcome = _failure(status, answer, write=False)
    _audit(CATALOGUE_TOOL, ctx, target, outcome, remote, reason=_reason(result))
    return {"tenant_id": target, "status": "error", **result}


async def _org_tools(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.cross_tenant import reachable_tenants

    remote = _remote_name(args)
    try:
        target = _admit(CATALOGUE_TOOL, args, ctx, ACCESS_READ, await _mode())
    except _RefusalError as refused:
        _audit(
            CATALOGUE_TOOL,
            ctx,
            refused.target,
            "denied",
            remote,
            reason=str(refused.result["error"]),
        )
        return refused.result
    tool = args.get("tool") if isinstance(args.get("tool"), str) else None
    if target:
        return await _catalogue_of(target, tool, ctx, remote)
    others = reachable_tenants(ctx)[1:]
    if not others:
        return {"tenants": [], "note": NO_REACH_NOTE}
    entries = await asyncio.gather(*(_catalogue_of(t, None, ctx, None) for t in others))
    return {"tenants": list(entries)}


HANDLERS[CATALOGUE_TOOL] = _org_tools
HANDLERS[READ_TOOL] = _org_tool_read
HANDLERS[WRITE_TOOL] = _org_tool_write
