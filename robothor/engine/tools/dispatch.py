"""Tool execution router — dispatches tool calls to handler modules."""

from __future__ import annotations

import asyncio
import logging
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

from robothor.constants import DEFAULT_TENANT
from robothor.engine.tools.response_failure import http_status_failure, tool_response_failure

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


# ── The agent's toolset, deferred or not ───────────────────────────────
# Published by the runner on EVERY run, so that `tool_search` has something
# true to answer with when the run is not deferred. It used to answer
# "tool_search is only available on deferred runs" — 18 times in one week on
# cron, event and sub-agent runs, to an agent that goes on calling it because
# it worked on the last run. A search over the tools the agent DOES have,
# ranked by the query, is an answer; a refusal is not.
#
# Read-only: unlike _thread_tool_whitelist it gates nothing and denies nothing.
_agent_toolset_var: ContextVar[frozenset[str] | None] = ContextVar(
    "_agent_toolset_var", default=None
)


def set_agent_toolset(names: frozenset[str]) -> Token[frozenset[str] | None]:
    """Record this run's allowed tool names; returns a reset token."""
    return _agent_toolset_var.set(names)


def clear_agent_toolset(token: Token[frozenset[str] | None]) -> None:
    """Restore the prior toolset record. Pair every set with one clear."""
    _agent_toolset_var.reset(token)


def get_agent_toolset() -> frozenset[str] | None:
    """The tool names this run is allowed, or None when nothing published."""
    return _agent_toolset_var.get()


@dataclass(frozen=True)
class ToolContext:
    """Context passed to every tool handler."""

    def get_service(self, name: str) -> Any:
        """A named service an installed package provides, or None.

        The counterpart to `ctx.get` in harnesses built on a service kernel.
        Without it a package could register a capability that nothing running
        inside the engine could reach, which is a registry rather than an
        extension point.
        """
        from robothor.engine.services import get_service as _get

        return _get(name)

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


def builtin_handlers() -> dict[str, Any]:
    """Every tool CORE ships, with no plugin anywhere near it.

    Split out of ``_collect_handlers`` so that "which tool names does the host
    already own" has ONE answer. ``loader.builtin_names("genus.tools")`` reads
    this, which is what lets the operator surfaces refuse a shadowing plugin
    for the same reason production does; a second list of built-in tool names
    is the drift `hardcoded-names-drift` documents. It must not consult the
    plugin loader — that is what makes it safe for the loader to call.
    """
    from robothor.engine.tools.handlers import (  # noqa: E501
        approvals,
        ask_user,
        attachments,
        benchmark,
        browser,
        code_exec,
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
        images,
        intents,
        jira,
        judge,
        mcp_client,
        memory,
        memory_vault,
        messaging,
        observability,
        pdf,
        reasoning,
        reports,
        sales,
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
        web_render,
    )

    all_handlers: dict[str, Any] = {}
    for mod in [
        memory,
        memory_vault,
        intents,
        symbolic,
        vision,
        web,
        web_render,
        filesystem,
        crm,
        browser,
        code_exec,
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
        messaging,
        skills,
        jira,
        judge,
        github_api,
        devops_metrics,
        identity,
        reports,
        sales,
        mcp_client,
        timing,
        todolist,
        toolsearch,
        approvals,
        ask_user,
        images,
        attachments,
    ]:
        all_handlers.update(mod.HANDLERS)

    return all_handlers


def _collect_handlers() -> dict[str, Any]:
    """Every tool the engine can dispatch: core's, then the plugins'."""
    all_handlers = builtin_handlers()

    # Third-party tools, last and never over the top of ours. `reserved_names`
    # is the built-in set: a plugin silently replacing `exec` or `write_file`
    # would be a takeover, not an extension. A plugin that fails to load is
    # recorded and skipped — one broken package must not stop the engine.
    from robothor.plugins import load_plugins

    plugins = load_plugins(reserved_names=set(all_handlers))
    for failure in plugins.failures:
        logger.warning("Plugin %r not loaded: %s", failure.name, failure.reason)
    all_handlers.update(plugins.tools)
    return all_handlers


# Lazily initialized handler map
_handler_map: dict[str, Any] | None = None
#: Plugin generation the map above was built at. A reload bumps the loader's
#: counter and this falls behind, so the next lookup rebuilds.
_handler_map_generation: int = -1


def _get_handlers() -> dict[str, Any]:
    global _handler_map, _handler_map_generation
    from robothor.plugins import generation

    current = generation()
    if _handler_map is None or _handler_map_generation != current:
        _handler_map = _collect_handlers()
        _handler_map_generation = current
    return _handler_map


#: An argument name looks like an identifier: short, no spaces. A KeyError on
#: anything else came from inside the handler and is a real bug.
_ARG_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,39}\Z")


def _describe_exception(e: BaseException) -> tuple[str, bool]:
    """Turn a handler exception into (message, crashed).

    A missing argument is a bad CALL, not an engine crash, and the reader is a
    model: it can recover from "missing required argument 'id'" on the next
    turn, and cannot do anything with "KeyError: 'id'".

    Found by walking the task lifecycle through the real registry —
    `delete_task` does a bare ``args["id"]`` and surfaced
    ``{"error": "KeyError: 'id'", "tool_crashed": true}``, while `get_task`
    beside it returns a sentence naming the problem and the tool to call
    instead. Forty-one bare ``args[...]`` accesses across the handler package
    behave like the former. Fixing them individually would be forty-one edits
    and a forty-second waiting to be written; every one of them already passes
    through here.

    Narrow on purpose. Only a KeyError whose key looks like an argument name
    is reclassified — a KeyError from a cache lookup deep inside a handler is
    still a crash, and still says so.
    """
    if isinstance(e, KeyError) and e.args:
        key = e.args[0]
        if isinstance(key, str) and _ARG_NAME_RE.match(key):
            return (
                f"missing required argument {key!r} — the call did not include it; "
                f"check the tool's schema and retry with {key!r} set",
                False,
            )
    return (f"{type(e).__name__}: {e}", True)


def normalise_arguments(
    arguments: dict[str, Any], properties: dict[str, Any] | None
) -> dict[str, Any]:
    """Match supplied argument names to declared parameters, ignoring case
    and underscores.

    The tool surface speaks two languages. Across the 172 tools that take
    parameters: 82 are snake_case only, 26 are camelCase only, and one mixes
    both within itself. So a model that has just called get_task({"id": ...})
    sends `to_agent` to send_notification, which declares `toAgent`.

    That failure is invisible. send_notification does not raise on an unknown
    key — it uses defaults, hits a database CHECK constraint, and returns
    "Failed to send notification". Nothing names the wrong argument, so the
    model has nothing to correct toward and retries the same shape.

    Normalising here fixes every tool at once and changes no schema, which is
    why it is done rather than renaming 26 public tool interfaces.

    Three rules keep it from guessing:
      * an exact match always wins, even if an alias is also present;
      * an argument with no declared match is passed through untouched — the
        handler may legitimately accept extras;
      * if two declared parameters normalise to the same key, neither is
        chosen. Silently picking one would be worse than the original error.
    """
    if not properties or not arguments:
        return arguments

    def key(name: str) -> str:
        return name.replace("_", "").lower()

    by_key: dict[str, list[str]] = {}
    for declared in properties:
        by_key.setdefault(key(declared), []).append(declared)

    out: dict[str, Any] = {}
    for given, value in arguments.items():
        if given in properties:
            out[given] = value
            continue
        candidates = by_key.get(key(given), [])
        if len(candidates) == 1 and candidates[0] not in arguments:
            out[candidates[0]] = value
        else:
            # No match, or ambiguous: leave it exactly as sent.
            out[given] = value
    return out


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
        from robothor.secrets.redaction import redact

        details: dict[str, Any] = {"tenant_id": tenant_id}
        if user_id:
            details["user_id"] = user_id
        if error:
            # Redacted, defence in depth. An audit row outlives the run and is
            # exported wholesale into a support bundle, and this `error` is
            # sometimes the text of an exception somebody else raised — the
            # shape `robothor/secrets/redaction.py` exists for, where a
            # credential arrives from outside and the process is not holding
            # anything to compare it against. The arguments were never here;
            # this closes the one field that could carry a value.
            details["error"] = redact(error)[:500]
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


async def _refuse_before_handler(
    name: str,
    *,
    agent_id: str,
    tenant_id: str,
    user_id: str,
    user_role: str,
) -> dict[str, Any] | None:
    """The two refusals that precede any handler: RBAC, then the fork whitelist.

    Extracted from ``_execute_tool`` when two merged branches pushed it past
    the 200-line function ratchet. Both gates answer the same question — may
    this call happen at all — and both answer it before a ``ToolContext``
    exists, so they belong together. Returns the structured refusal to hand
    back, or ``None`` when the call may proceed.
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

    return None


async def _runtime_denial(
    name: str, args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any] | None:
    from robothor.goals.runtime import admit_tool

    try:
        from robothor.engine.runtime.controls import stopped
        from robothor.engine.runtime.deadlines import require_time

        require_time()
        if ctx.is_benchmark:
            # A benchmark child has no durable stop to honour, no resume claim
            # and no goal to admit against — `effect_dispatch._bypass` already
            # excludes it from the durable ledger for exactly those reasons.
            # It matters more than consistency: every check below READS THE
            # DATABASE, and the benchmark sandbox's guarantee is that such a
            # run touches none. The read raised inside this handler and was
            # returned as a crash result, so the sandbox refusal the caller
            # expected never happened and the write appeared to reach the DB.
            return None
        from robothor.engine.resume_claim import current as resume_claim
        from robothor.engine.resume_claim import require_owned

        if resume_claim.get() is not None:
            await asyncio.to_thread(require_owned)
        if ctx.run_id and await asyncio.to_thread(stopped, ctx.tenant_id, ctx.run_id):
            raise ValueError(
                "durable stop denies further tool dispatch; reconcile in-flight effects"
            )
        await asyncio.to_thread(admit_tool, name, args, ctx)
    except Exception as exc:
        err_msg, crashed = _describe_exception(exc)
        _audit_tool_call(
            name, ctx.agent_id, ctx.tenant_id, user_id=ctx.user_id, status="denied", error=err_msg
        )
        return {"error": err_msg, "tool_crashed": True} if crashed else {"error": err_msg}

    return None


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
    # ── The two gates that can refuse before a handler context exists ──
    refusal = await _refuse_before_handler(
        name, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id, user_role=user_role
    )
    if refusal is not None:
        return refusal

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
    denial = await _runtime_denial(name, args, ctx)
    if denial:
        return denial

    from robothor.engine.runtime.effect_dispatch import invoke

    return await invoke(name, args, ctx, lambda: _dispatch_admitted(name, args, ctx))


async def _dispatch_admitted(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    agent_id, run_id, tenant_id = ctx.agent_id, ctx.run_id, ctx.tenant_id
    user_id, workspace, is_benchmark = ctx.user_id, ctx.workspace, ctx.is_benchmark
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
            return tool_response_failure(
                name, {"error": f"Adapter tool '{name}' failed: {e}", "retryable": True}
            )

    handlers = _get_handlers()
    handler = handlers.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}

    # ── Repeat-call guard ──
    # The one place every tool call passes through BEFORE the handler runs, so
    # a call the run has already made and already been answered the same way
    # can be answered from what it has. Read-only allow-list, never skips a
    # write, and below `enforce` it decides nothing and only logs. Per-run
    # state lives on the run's session (robothor/engine/repeat_guard.py); a
    # call from outside a live run, or a run at `off`, finds no guard at all
    # and pays nothing.
    #
    # Deliberately ABOVE the benchmark-sandbox token: its reset lives in the
    # `finally` of the handler's try, and an early return from here used to
    # jump straight over it, leaving the DAL's ContextVar True for the rest of
    # the task — "CRM writes silently sandboxed" being the failure mode.
    from robothor.engine.repeat_guard import guard_for_run

    guard = guard_for_run(run_id)
    if guard is not None:
        decision = await asyncio.to_thread(guard.before, name, args, workspace=workspace)
        if decision is not None and decision.result is not None:
            _audit_tool_call(
                name,
                agent_id,
                tenant_id,
                user_id=user_id,
                status="ok" if decision.action == "answered" else "denied",
                error=decision.note if decision.action == "refused" else None,
            )
            return decision.result

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
        return http_status_failure(name, status, err_msg)
    except httpx.HTTPError as e:
        # Transport-level failure (connect refused, timeout, protocol error)
        # from a handler that didn't route through service_client — e.g. the
        # memory tools when the embedding service is down. An operational
        # state, not a bug: one warning line, no traceback, no crash flag.
        err_msg = f"backing service unreachable: {type(e).__name__}"
        logger.warning("Tool %s: %s", name, err_msg)
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="error", error=err_msg)
        return tool_response_failure(name, {"error": err_msg, "retryable": True})
    except Exception as e:
        err_msg, crashed = _describe_exception(e)
        if crashed:
            logger.exception("Tool %s raised unhandled exception", name)
        else:
            # A bad call, not a bug: one line, no traceback. Still audited as
            # an error and still a failed call — only the message changes.
            logger.warning("Tool %s: %s", name, err_msg)
        _audit_tool_call(name, agent_id, tenant_id, user_id=user_id, status="error", error=err_msg)
        return {"error": err_msg, "tool_crashed": True} if crashed else {"error": err_msg}
    finally:
        if sandbox_token is not None:
            from robothor.crm.dal import reset_benchmark_sandbox

            reset_benchmark_sandbox(sandbox_token)
    # Trusted workflow evidence sees the native handler's result, never a
    # model-supplied claim or a later verification annotation. Cached repeats
    # are not new retrievals; their original execution was already observed.
    from robothor.engine.tool_observation import observe_tool_result

    workflow_context = observe_tool_result(name, args, result, ctx)
    if workflow_context is not None:
        if not isinstance(result, dict) or "_workflow_context" in result:
            raise ValueError("Native result cannot accept reserved workflow context")
        result = {**result, "_workflow_context": workflow_context}

    # ── Repeat-call guard: remember what this call returned ──
    # Deliberately BEFORE verification, so what the guard digests is the
    # handler's output plus optional trusted workflow context, before verification.
    if guard is not None:
        await asyncio.to_thread(guard.after, name, args, result, workspace=workspace)

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
