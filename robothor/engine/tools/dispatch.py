"""Tool execution router — dispatches tool calls to handler modules."""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from robothor.config import Config
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)


# ── Per-task tool whitelist (Rip 1 background-review fork) ──────────
# When set, _execute_tool denies any tool call whose name is not in
# the whitelist before either adapter routing or handler dispatch
# happens. ContextVar gives async-safe per-task isolation — the
# whitelist set in a forked review task does NOT leak into the parent
# task or sibling forks.
#
# Default None means "no restriction" — foreground agent behaviour is
# unchanged. Use set_tool_whitelist() to install, and pass the
# returned Token to clear_tool_whitelist() in a finally block.
_thread_tool_whitelist: ContextVar[frozenset[str] | None] = ContextVar(
    "_thread_tool_whitelist", default=None
)


def set_tool_whitelist(allowed: frozenset[str]) -> Token[frozenset[str] | None]:
    """Install a per-task tool whitelist; returns reset token.

    Tool calls outside ``allowed`` will return a structured "denied"
    error from ``_execute_tool``. The whitelist applies only within
    the current asyncio Task (and any tasks it explicitly spawns
    that inherit context).
    """
    return _thread_tool_whitelist.set(allowed)


def clear_tool_whitelist(token: Token[frozenset[str] | None]) -> None:
    """Restore the prior whitelist state. Pair every set with one clear."""
    _thread_tool_whitelist.reset(token)


def get_tool_whitelist() -> frozenset[str] | None:
    """Inspect the currently-installed whitelist, if any."""
    return _thread_tool_whitelist.get()


# ── Deferred-tools allow-set (Rip 16 / G4) ─────────────────────────
# When an agent's toolset is deferred, the runner records the agent's full
# allowed tool set here. The tool_call meta-tool consults it so a discovered
# tool outside the allow-list cannot be invoked (tools_denied is otherwise only
# enforced by the advertised schema list, which deferral shrinks to core+meta).
#
# This is deliberately SEPARATE from _thread_tool_whitelist: it gates only
# tool_call, not all dispatch, so it cannot wrongly restrict a non-deferring
# sub-agent that inherits the parent task's context. Default None.
_deferred_allowed_var: ContextVar[frozenset[str] | None] = ContextVar(
    "_deferred_allowed_var", default=None
)


def set_deferred_allowed(allowed: frozenset[str]) -> Token[frozenset[str] | None]:
    """Record the deferred-run allow-set; returns a reset token."""
    return _deferred_allowed_var.set(allowed)


def clear_deferred_allowed(token: Token[frozenset[str] | None]) -> None:
    """Restore the prior deferred allow-set state."""
    _deferred_allowed_var.reset(token)


def get_deferred_allowed() -> frozenset[str] | None:
    """The current deferred-run allow-set, or None when not a deferred run."""
    return _deferred_allowed_var.get()


@dataclass(frozen=True)
class ToolContext:
    """Context passed to every tool handler."""

    agent_id: str = ""
    run_id: str = ""  # current AgentRun id — lets handlers find per-run state
    tenant_id: str = field(default_factory=lambda: DEFAULT_TENANT)
    workspace: str = ""
    user_id: str = ""
    user_role: str = ""
    accessible_tenant_ids: tuple[str, ...] = ()
    # Task authorship override: when set, CRM task handlers attribute
    # filed/updated tasks to this identity instead of agent_id. Used by
    # the scout beat (runs as agent_id='main' but files as 'scout').
    task_author_override: str = ""
    # Benchmark sandbox marker — copied from AgentRun.is_benchmark when the
    # runner builds the ctx. Side-effecting tool handlers (notably the gws
    # CLI wrapper which sits outside the runner's allow-list guard) check
    # this to short-circuit mutations.
    is_benchmark: bool = False
    # The run's resolved IdentityContext (Task 2), or None for system/cron/
    # heartbeat runs that never resolve an interactive identity — see
    # ``robothor.engine.runner``'s ``effective_identity`` / ``session.identity``.
    # Data-read handlers use this (via ``robothor.identity.scope.scope_for``)
    # to compute the "own data + shared" DataScope for a call; None is the
    # unaffected, pre-Task-5 default every existing caller gets.
    identity: IdentityContext | None = None


def get_db() -> Any:
    """Standard DB connection for tool handlers.

    Usage::

        with get_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            conn.commit()
    """
    from robothor.db.connection import get_connection

    return get_connection()


def _cfg() -> Config:
    """Lazy config access (not module-level to avoid import-time side effects)."""
    from robothor.config import get_config

    return get_config()


def _collect_handlers() -> dict[str, Any]:
    """Collect all HANDLERS dicts from handler modules."""
    from robothor.engine.tools.handlers import (  # noqa: E501
        apollo,
        benchmark,
        browser,
        crm,
        desktop,
        devops_metrics,
        experiment,
        federation,
        filesystem,
        git,
        github_api,
        goal,
        gws,
        identity,
        intents,
        jira,
        judge,
        mcp_client,
        memory,
        memory_vault,
        messaging,
        observability,
        pdf,
        pf,
        reasoning,
        reports,
        skills,
        spawn,
        symbolic,
        timing,
        todolist,
        toolsearch,
        vault,
        vision,
        voice,
        web,
    )

    all_handlers: dict[str, Any] = {}
    for mod in [
        memory,
        memory_vault,
        intents,
        symbolic,
        vision,
        web,
        apollo,
        filesystem,
        crm,
        browser,
        desktop,
        experiment,
        benchmark,
        goal,
        git,
        gws,
        vault,
        observability,
        voice,
        spawn,
        pdf,
        reasoning,
        federation,
        pf,
        messaging,
        skills,
        jira,
        judge,
        github_api,
        devops_metrics,
        identity,
        reports,
        mcp_client,
        timing,
        todolist,
        toolsearch,
    ]:
        all_handlers.update(mod.HANDLERS)
    return all_handlers


# Lazily initialized handler map
_handler_map: dict[str, Any] | None = None


def _get_handlers() -> dict[str, Any]:
    global _handler_map
    if _handler_map is None:
        _handler_map = _collect_handlers()
    return _handler_map


def _audit_tool_call(
    tool_name: str,
    agent_id: str,
    tenant_id: str,
    *,
    user_id: str = "",
    status: str = "ok",
    error: str | None = None,
) -> None:
    """Record a tool invocation in the audit log (non-blocking, never raises)."""
    try:
        from robothor.audit.logger import log_event

        details: dict[str, Any] = {"tenant_id": tenant_id}
        if user_id:
            details["user_id"] = user_id
        if error:
            details["error"] = error[:500]
        log_event(
            event_type="agent.tool_call",
            action=tool_name,
            category="agent",
            actor=agent_id or "unknown",
            user_id=user_id,
            details=details,
            status=status,
        )
    except Exception:
        pass


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    run_id: str = "",
    tenant_id: str = "",
    workspace: str = "",
    user_id: str = "",
    user_role: str = "",
    accessible_tenant_ids: tuple[str, ...] = (),
    task_author_override: str = "",
    is_benchmark: bool = False,
    identity: IdentityContext | None = None,
) -> dict[str, Any]:
    """Route tool call to the correct handler.

    Checks user permissions, then adapter-provided tools (dynamic MCP
    servers), then falls through to hardcoded engine handlers.
    """
    # ── Permission check (single enforcement gate) ──
    from robothor.engine.permissions import check_tool_permission

    # Permission lookup is database-backed and synchronous.  Keep it off the
    # Engine event loop so one slow RBAC query cannot stall unrelated chat,
    # health, or agent sessions.
    denied = await asyncio.to_thread(
        check_tool_permission, user_role, tenant_id, name, user_id=user_id
    )
    if denied:
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="denied", error=denied)
        return {"error": denied}

    # ── Per-task tool whitelist (Rip 1) ──
    # If a parent forked us with a restricted toolset (e.g. the
    # background-review fork that may only touch memory + skills),
    # bounce anything outside that set with a structured error before
    # the handler ever sees it.
    whitelist = _thread_tool_whitelist.get()
    if whitelist is not None and name not in whitelist:
        msg = f"Tool '{name}' denied by per-task whitelist"
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="denied", error=msg)
        return {"error": msg, "denied_by_whitelist": True}

    from robothor.engine.tools import get_registry

    route = get_registry().get_adapter_route(name)
    if route:
        from robothor.engine.mcp_client import get_mcp_client_pool

        try:
            pool = get_mcp_client_pool()
            session = await pool.get_session(route)
            result: dict[str, Any] = await session.call_tool(name, args)
            _audit_tool_call(name, agent_id, tenant_id, user_id=user_id)
            return result
        except Exception as e:
            logger.error("Adapter tool %s (server=%s) failed: %s", name, route, e)
            _audit_tool_call(
                name, agent_id, tenant_id, user_id=user_id, status="error", error=str(e)
            )
            return {"error": f"Adapter tool '{name}' failed: {e}"}

    ctx = ToolContext(
        agent_id=agent_id,
        run_id=run_id,
        tenant_id=tenant_id,
        workspace=workspace,
        user_id=user_id,
        user_role=user_role,
        accessible_tenant_ids=accessible_tenant_ids,
        task_author_override=task_author_override,
        is_benchmark=is_benchmark,
        identity=identity,
    )
    handlers = _get_handlers()
    handler = handlers.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}

    # Benchmark sandbox: mirror ctx.is_benchmark into the CRM DAL's
    # ContextVar for the duration of the handler call. DAL paths that
    # create operator-facing state (dal.create_session_goal) cannot see
    # ToolContext, so this is how the sandbox reaches them regardless of
    # which handler (create_goal, thread machinery, …) invoked the write.
    sandbox_token = None
    if is_benchmark:
        from robothor.crm.dal import set_benchmark_sandbox

        sandbox_token = set_benchmark_sandbox(True)

    # Wrap handler invocation: an unhandled exception here used to propagate
    # out of the runner, leaving agent_runs rows in 'running' state until the
    # 30-min reaper fired. Returning a structured error lets the LLM decide
    # whether to retry, skip, or surface to the operator.
    try:
        result = cast("dict[str, Any]", await handler(args, ctx))
    except httpx.HTTPStatusError as e:
        # A backing service responded with an error status. Map to a short
        # structured error: the raw exception text embeds the internal
        # loopback URL, which must never reach agent context.
        status = e.response.status_code
        err_msg = f"backing service error (HTTP {status})"
        logger.warning("Tool %s: %s", name, err_msg)
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="error", error=err_msg)
        return {"error": err_msg, "retryable": status >= 500}
    except httpx.HTTPError as e:
        # Transport-level failure (connect refused, timeout, protocol error)
        # from a handler that didn't route through service_client — e.g. the
        # memory tools when the embedding service is down. An operational
        # state, not a bug: one warning line, no traceback, no crash flag.
        err_msg = f"backing service unreachable: {type(e).__name__}"
        logger.warning("Tool %s: %s", name, err_msg)
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="error", error=err_msg)
        return {"error": err_msg, "retryable": True}
    except Exception as e:
        logger.exception("Tool %s raised unhandled exception", name)
        err_msg = f"{type(e).__name__}: {e}"
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="error", error=err_msg)
        return {"error": err_msg, "tool_crashed": True}
    finally:
        if sandbox_token is not None:
            from robothor.crm.dal import reset_benchmark_sandbox

            reset_benchmark_sandbox(sandbox_token)
    # ── Post-condition verification (grade the environment, not the transcript) ──
    # The single choke point every tool call passes through, AFTER the handler
    # has returned successfully. Bookkeeping only: verify_tool_result never
    # raises and, below the enforce rung, returns the result untouched. The
    # try/except guards the import itself, so even a broken verification module
    # cannot fail an agent's real work.
    try:
        from robothor.engine.tools import verification

        result = await verification.verify_tool_result(name, args, result, ctx)
    except Exception as e:  # noqa: BLE001
        logger.warning("Post-condition verification skipped for %s: %s", name, e)

    if isinstance(result, dict) and "error" in result:
        _audit_tool_call(
            name, agent_id, tenant_id, user_id=user_id, status="error", error=result["error"]
        )
    else:
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id)
    return result
