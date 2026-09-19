"""
Agent Runner — core LLM conversation loop with tool calling.

Uses litellm for unified LLM API with model fallback.
Executes tools directly via the ToolRegistry (DAL calls, no HTTP).

v2 enhancements (all guarded by config flags, default off):
  - Error feedback loop (default: on)
  - Token/cost budget controls
  - Planning phase
  - Scratchpad / working memory
  - Graduated escalation
  - Guardrails framework
  - Checkpointing / resume
  - Self-validation / verify step
  - Structured telemetry
  - Difficulty-aware routing

Usage:
    runner = AgentRunner(engine_config)
    run = await runner.execute("email-classifier", "Process triage inbox")
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import traceback
from typing import TYPE_CHECKING, Any

import litellm

from robothor.db.connection import current_tenant_scope
from robothor.engine.cancel_outcome import _cancel_outcome, terminal_run
from robothor.engine.checkpoint import save_iteration
from robothor.engine.config import (
    EngineConfig,
    _prompt_cache,
    build_system_prompt,
    load_agent_config_or_reason,
)

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.context_budget import keep_context_within_budget

# Re-exported for existing importers. The `as` form is what marks a name as
# deliberately re-exported; a plain import reads to mypy as a private detail,
# which is the right default and the wrong one here.
from robothor.engine.deliverables import task_text_from  # noqa: E402
from robothor.engine.error_actions import apply_error_recovery, inject_error_feedback
from robothor.engine.finalization_budget import FinalizationBudget  # noqa: E402
from robothor.engine.injection_screen import screen_run_prompt
from robothor.engine.journal_resume import maybe_prepend_journal_resume
from robothor.engine.last_resort import all_models_failed

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.llm_client import LLMClient  # noqa: E402
from robothor.engine.loop_guards import (
    GuardState,
    append_engine_note,
    check_iteration_guards,
    nudge_for_missing_deliverable,
)
from robothor.engine.models import (
    AgentConfig,
    AgentRun,
    RunStep,
    SpawnContext,
    StepType,
    TriggerType,
)
from robothor.engine.prompts import (
    EXECUTION_MODE_PREAMBLE,
)
from robothor.engine.run_budget import (  # noqa: E402
    DEADLINE_WARNING_FRACTION as DEADLINE_WARNING_FRACTION,
)
from robothor.engine.run_budget import (
    deadline_warning as deadline_warning,
)
from robothor.engine.run_budget import (
    proactive_compaction_threshold as proactive_compaction_threshold,
)
from robothor.engine.run_budget import watchdog_budgets_for
from robothor.engine.run_context import mark_benchmark_run
from robothor.engine.run_deadline import (
    BudgetStop,
    RunBudgetError,
    begin_wrapup,
    end_run_at_budget,
    wrapup_note,
    wrapup_schemas,
)
from robothor.engine.run_finalizer import RunFinalizationMixin
from robothor.engine.run_identity import resolve_run_identity
from robothor.engine.run_lifecycle import RunLifecycleMixin, spawn_post_stall_autodream
from robothor.engine.run_llm_calls import LLMCallMixin  # noqa: E402
from robothor.engine.run_pacing import DeadlinePacer, checkin_note, mode_for_run  # noqa: E402
from robothor.engine.run_replan import maybe_replan  # noqa: E402
from robothor.engine.sandbox_policy import agent_holds_exec, resolve_sandbox_decision
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.session import ENGINE_CONTEXT_ROLE, AgentSession
from robothor.engine.stall_watchdog import (
    _active_watchdog_var,
    _build_cancel_diagnostic,
    _StallWatchdog,
)
from robothor.engine.tool_admission import ToolAdmissionMixin  # noqa: E402
from robothor.engine.tool_timeouts import (  # noqa: E402
    HARNESS_BUDGETED_TOOLS,
    LONG_RUNNING_TOOLS,
    resolve_tool_timeout,
)
from robothor.engine.tool_turn import ToolTurnMixin, ToolTurnRequest  # noqa: E402
from robothor.engine.tools import get_registry
from robothor.engine.toolset_prep import (
    planner_tool_names,
    prepare_toolset,
    publish_toolset,
    with_discovery_note,
    withdraw_toolset,
)
from robothor.engine.tracking import create_run, update_run
from robothor.engine.warmup_steps import record_warmup_steps
from robothor.engine.workflow_budget import WorkflowDeadlineError, propagates_to_caller

# Per-tool wall-clock caps. The tables and the rule live in
# robothor/engine/tool_timeouts.py; re-exported under their old private names
# because four modules and six test files reach for them here, and because the
# reason they moved is that FOUR verbatim copies had accumulated and three had
# already drifted (`ask_user` was in this one and in none of the others).
_HARNESS_BUDGETED_TOOLS = HARNESS_BUDGETED_TOOLS
_LONG_RUNNING_TOOLS = LONG_RUNNING_TOOLS
_resolve_tool_timeout = resolve_tool_timeout


#: How stale an interactive preamble may be before the next turn re-warms.
#: The old gate was "history is empty", which never fires on a persistent
#: session: main.yaml sets session_target: persistent and that session holds
#: 5,560 messages. Measured over 30 days, cron runs executed 11.0 warmup
#: sections each while telegram runs executed 0.0 — the operator's own
#: conversations loaded no memory blocks, preferences or breadcrumbs at all.
INTERACTIVE_WARMUP_MAX_AGE_S = 900


def _seconds_since_last_interactive_run(agent_id: str, tenant_id: str) -> float | None:
    """Seconds since this agent's previous interactive run, or None if there is none.

    Best-effort: on any error the caller warms, which is the safe direction —
    a redundant preamble costs latency, a missing one costs the operator their
    memory context.
    """
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))
                FROM agent_runs
                WHERE agent_id = %s AND tenant_id = %s
                  AND trigger_type IN ('telegram', 'webchat')
                """,
                (agent_id, tenant_id),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:  # noqa: BLE001 — never block a turn on this
        logger.debug("interactive warmup recency lookup failed: %s", _sanitize(exc))
        return None


def should_warm_interactive(*, history_len: int, seconds_since_warmup: float | None) -> bool:
    """Whether an interactive turn should build the warmup preamble.

    First turn of a session always warms. After that, warm again once the last
    preamble is older than ``INTERACTIVE_WARMUP_MAX_AGE_S`` — a conversation
    resumed hours later gets fresh memory, a rapid back-and-forth does not pay
    for it on every turn.

    The old comment claimed follow-ups inherit memory blocks from conversation
    history. They do not: the preamble is prepended to a local variable and
    never persisted to the session, so there is nothing for a follow-up to
    inherit.
    """
    if history_len <= 0:
        return True
    if seconds_since_warmup is None:
        return True
    return seconds_since_warmup > INTERACTIVE_WARMUP_MAX_AGE_S


# Init timeout: max seconds for agent setup before first LLM call.
# Agents that hang during warmup, adapter loading, or tool registration
# are killed immediately.  Prevents the "stuck in initialization"
# failure mode where runs sit for 30+ minutes with 0 tokens consumed.
INIT_TIMEOUT_SECONDS = 60

# Defined before the env-tunable constants below: _int_env_with_fallback and
# the soft>=hard sanity check run AT IMPORT TIME, so the logger must already
# exist on a fresh import (reload-based tests mask this — the old module dict
# keeps a stale binding alive).
logger = logging.getLogger(__name__)


def _int_env_with_fallback(name: str, default: int) -> int:
    """Read an int env var; fall back to ``default`` on missing/garbage values.

    Read once at module import (see call sites below) — these are fleet-wide
    thresholds, not per-run config, so there's no need to re-read per call.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r (expected an integer); falling back to default %d",
            name,
            raw,
            default,
        )
        return default
    if value <= 0:
        # A zero/negative hard cap would trip `used >= cap` at iteration 0
        # of every run (instant budget_exhausted, fleet-wide). Never honor
        # non-positive thresholds.
        logger.warning(
            "Invalid %s=%r (must be a positive integer); falling back to default %d",
            name,
            raw,
            default,
        )
        return default
    return value


# Fleet-wide runaway-token thresholds. Applied to the cumulative
# session.run.input_tokens + session.run.output_tokens across the run.
#   - Crossing ALERT fires a (batched — see RUNAWAY_SOFT_ALERT_WINDOW_SECONDS
#     below) Telegram warning so the operator can decide whether to intervene.
#   - Reaching HARD_CAP stops the loop cleanly with budget_exhausted=True.
# These are fleet-wide, env-tunable but NOT per-agent-manifest-configurable,
# so a misconfigured manifest can never disable the protection. A main run at
# Apr 22 16:07 consumed 3.2M input tokens before hitting the 86400s circuit
# breaker; this guard would have stopped it at 5M.
RUNAWAY_TOKEN_ALERT = _int_env_with_fallback("ROBOTHOR_RUNAWAY_ALERT_TOKENS", 500_000)
RUNAWAY_TOKEN_HARD_CAP = _int_env_with_fallback("ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS", 5_000_000)

if RUNAWAY_TOKEN_ALERT >= RUNAWAY_TOKEN_HARD_CAP:
    # Still safe (the hard cap always protects), but the soft-alert branch
    # becomes unreachable — say so at startup instead of failing silently.
    logger.warning(
        "ROBOTHOR_RUNAWAY_ALERT_TOKENS (%d) >= ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS (%d): "
        "soft alerts will never fire — runs hit the hard cap first.",
        RUNAWAY_TOKEN_ALERT,
        RUNAWAY_TOKEN_HARD_CAP,
    )

# Soft-alert batching: post-recovery catch-up runs routinely cross the soft
# threshold several times in quick succession (legitimate backlog burn,
# contained by the hard cap) — paging once per run turned 6 runs in 90
# minutes into 6 pages for ~$0.35 of working-as-designed spend (2026-08-19).
# At most one page fires per quiet-period boundary: the first soft-runaway
# event after a quiet window pages immediately (with context); everything
# else within the window accumulates silently and is reported as a single
# summary the next time a soft event lands after the window has expired.
# Hard-cap alerts are NOT subject to this — they always page immediately.
RUNAWAY_SOFT_ALERT_WINDOW_SECONDS = 3600

# Module-level batching registry. Touched only from the single asyncio event
# loop that drives agent runs (_run_loop is `await`ed, never threaded), and
# _send_soft_runaway_alert() itself has no `await` in its body — so it runs
# to completion atomically with respect to other coroutines on the loop.
# No locks needed.
_soft_runaway_window_started_at: float | None = None
_soft_runaway_pending: list[dict[str, Any]] = []


def _runaway_alert_clock() -> float:
    """Indirection point so tests can fake elapsed time without real sleeps."""
    return time.monotonic()


def _send_soft_runaway_alert(
    agent_id: str,
    run_id: str,
    tokens: int,
    model_used: str | None,
    cost_usd: float,
) -> None:
    """Fire-and-forget soft-runaway alert, batched to at most one per window.

    Sync, not async: the whole decision + dispatch happens without an
    `await`, which is what makes the module-level state safe to touch from
    any coroutine on the loop without a lock (see module docstring above).
    """
    global _soft_runaway_window_started_at, _soft_runaway_pending

    from robothor.engine.alerts import alert as _alert
    from robothor.engine.alerts import note_benchmark_runaway
    from robothor.engine.run_context import in_benchmark_run
    from robothor.engine.task_registry import get_task_registry

    # A graded child never joins the batch below and never opens its window:
    # `_soft_runaway_pending` is module-global and flushed by whichever run
    # crosses next IN THAT RUN'S CONTEXT, so a benchmark child flushing a
    # production batch would relabel a real page `benchmark_digest` and lose it.
    if in_benchmark_run():
        note_benchmark_runaway(agent_id, run_id, tokens, model_used)
        return

    now = _runaway_alert_clock()
    window_active = (
        _soft_runaway_window_started_at is not None
        and (now - _soft_runaway_window_started_at) < RUNAWAY_SOFT_ALERT_WINDOW_SECONDS
    )

    if window_active:
        # Within an active window: accumulate silently, no page.
        _soft_runaway_pending.append(
            {"agent": agent_id, "run_id": run_id, "tokens": tokens, "ts": now}
        )
        return

    if _soft_runaway_pending:
        # Window expired with events accrued while it was open. This event
        # joins them and the whole batch is flushed as ONE summary page
        # (never an individual page — that would defeat the batching), and
        # a fresh window opens. Including the trigger in the summary means
        # no crossing is ever dropped from alerting.
        #
        # Known trade-off: events that accrue in a window with NO subsequent
        # soft event stay pending until the next crossing, however far away
        # that is. Each crossing is still logger.warning'd per-run at the
        # call site, and the hard cap contains the runs themselves — only
        # the page is deferred, never the protection.
        pending = [
            *_soft_runaway_pending,
            {"agent": agent_id, "run_id": run_id, "tokens": tokens, "ts": now},
        ]
        _soft_runaway_pending = []
        _soft_runaway_window_started_at = now
        count = len(pending)
        run_list = ", ".join(f"{e['agent']} ({e['tokens']:,} tok)" for e in pending[:10])
        if count > 10:
            run_list += f", +{count - 10} more"
        body = (
            f"{count} run{'s' if count != 1 else ''} crossed the soft token "
            f"threshold ({RUNAWAY_TOKEN_ALERT:,}) since the last page: {run_list}. "
            f"All contained by the budget guard (hard cap {RUNAWAY_TOKEN_HARD_CAP:,})."
        )
        get_task_registry().spawn(
            _alert("info", "Runaway-token alerts (batched summary)", body),
            name=f"runaway-alert-summary:{agent_id}",
        )
        return

    # Quiet period: first soft-runaway event in a while. Page immediately,
    # with enough context to read severity at a glance — contained-by-guard
    # and approximate cost up front, so this doesn't read as an emergency.
    _soft_runaway_window_started_at = now
    cost_note = f"~${cost_usd:.2f} (negligible)" if cost_usd else "negligible"
    body = (
        f"run_id={run_id} tokens={tokens:,} (soft threshold {RUNAWAY_TOKEN_ALERT:,}, "
        f"hard cap {RUNAWAY_TOKEN_HARD_CAP:,}) model={model_used}. "
        f"Contained by the budget guard — cost so far {cost_note}. "
        f"Further soft alerts batched for {RUNAWAY_SOFT_ALERT_WINDOW_SECONDS // 60}min."
    )
    get_task_registry().spawn(
        _alert("warning", f"Runaway-token alert: {agent_id}", body),
        name=f"runaway-alert:{agent_id}",
    )


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.identity import IdentityContext

# Trigger types that run with no interactive human and are therefore governed by
# the agent's service_role under the RBAC ladder (see the system-run gate in
# _run_loop). This is an ALLOWLIST on purpose: interactive surfaces (telegram,
# webchat, slack, ide, manual, webhook) are gated by the dispatch
# user_role check instead, and any future trigger type defaults to that
# restrictive path rather than silently inheriting allow-all service_role.
_SYSTEM_TRIGGER_TYPES = frozenset(
    {
        TriggerType.CRON,
        TriggerType.HOOK,
        TriggerType.EVENT,
        TriggerType.WORKFLOW,
        TriggerType.SUB_AGENT,
        TriggerType.FEDERATION,
        TriggerType.CHANNEL_EVENT,
    }
)


def _is_service_caller(user_role: str, user_id: str) -> bool:
    """Whether this run's effective caller is a service/automated actor.

    A WEBCHAT run can still arrive from a service-typ auth context (an
    engine/bridge credential acting on an agent's behalf, not a human — see
    ``AuthContext.is_service`` at the chat layer). ``chat.py`` already passes
    ``identity=None`` for those, but the runner can't tell "deliberately
    None" from "not provided", so the fallback below must re-derive
    service-ness itself from the same conventions used elsewhere in this
    module: the manifest's default ``service_role`` value of ``"service"``
    (``AgentConfig.service_role``, ``issue_service_token``'s default role)
    and the ``f"service:{agent_id}"`` / ``f"service:workflow:{id}"`` user_id
    marker convention (``_SYSTEM_TRIGGER_TYPES`` branch above, workflow.py,
    scheduler.py). Without this gate, a service caller's non-UUID user_id
    reaches ``resolve_identity("webchat", ...)`` and triggers a DB error on
    every single call until the negative cache absorbs it (60s TTL).
    """
    return (
        user_role == "service" or user_role.startswith("service:") or user_id.startswith("service:")
    )


# Suppress litellm's verbose logging
litellm.suppress_debug_info = True

# Register custom pricing so litellm.completion_cost() prices our models.
# Single-sourced from model_registry._MODEL_REGISTRY (G6) when Rip 17 is on;
# otherwise the legacy two-model block, preserved inside the function.
from robothor.engine.model_registry import register_pricing_with_litellm  # noqa: E402

register_pricing_with_litellm()


def should_create_auto_task(config: AgentConfig, spawn_context: SpawnContext | None) -> bool:
    """True when this run should file its operator-facing ``auto_task`` CRM row.

    Three conditions, all necessary:

    - the agent asked for it (``auto_task``);
    - it is not a sub-agent run (children never file their own task);
    - it is not a benchmark run.

    The benchmark clause plugs a hole in the existing ``is_benchmark`` sandbox:
    ``tools/handlers/crm.py`` already refuses every task-mutating *tool* when
    ``ctx.is_benchmark``, but this write goes straight to the DAL and so never
    met that guard. 6,887 "<Agent>: sub_agent run" rows reached the operator's
    task queue that way, and the failed/timed-out ones sat there as TODO.
    """
    if not config.auto_task or spawn_context is not None:
        return False
    return not getattr(config, "is_benchmark", False)


# Sandbox policy lives in robothor/engine/sandbox_policy.py. Re-exported here
# because callers and tests already import these names from the runner, and
# because `sandbox: host` silently beating `enforce` deserved its own module
# with its own tests rather than ten more lines in a god-object.
_agent_holds_exec = agent_holds_exec
_resolve_sandbox_decision = resolve_sandbox_decision


#: Fraction of a run's wall-clock ceiling at which the agent is told to wrap
#: up. At 80% of a 900-second budget there are three minutes left — a few
#: tool calls at the measured rate of roughly six seconds each, which is
#: enough to write out what has been gathered. A warning at 95% is one the
#: agent cannot act on.


class AgentRunner(
    LLMCallMixin,
    RunLifecycleMixin,
    RunFinalizationMixin,
    ToolAdmissionMixin,
    ToolTurnMixin,
):
    """Executes agents: builds prompt, enters tool loop, tracks everything."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.registry = get_registry()
        # LLM dispatch/fallback/cost/streaming + message hygiene. Extracted
        # from this class (Phase A / Slice 1); stateless across runs.
        self._llm = LLMClient()

    async def execute(
        self,
        agent_id: str,
        message: str,
        trigger_type: TriggerType = TriggerType.MANUAL,
        trigger_detail: str | None = None,
        correlation_id: str | None = None,
        agent_config: AgentConfig | None = None,
        on_content: Callable[[str], Awaitable[None]] | None = None,
        on_tool: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_status: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        model_override: str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        resume_from_run_id: str | None = None,
        spawn_context: SpawnContext | None = None,
        readonly_mode: bool = False,
        execution_mode: bool = False,
        deep_plan: bool = False,
        tenant_id: str | None = None,
        user_id: str = "",
        user_role: str = "",
        identity: IdentityContext | None = None,
    ) -> AgentRun:
        """Execute an agent with the given message.

        Args:
            execution_mode: When True, prepend EXECUTION_MODE_PREAMBLE to
                system prompt to enforce plan execution (no re-planning).
            identity: Unified identity context (``robothor.identity``) for the
                human on the other end of an interactive run. Precedence when
                unset: WEBCHAT triggers resolve it from ``user_id``/``tenant_id``
                (skipped entirely for a service-like caller — see
                ``_is_service_caller`` — since a service token has no human
                behind it and its user_id is never a resolvable UUID);
                TELEGRAM triggers fall back to the legacy `trigger_detail`
                `|sender:` parse; a spawned child inherits its parent's via
                ``spawn_context.identity`` (attribution only — a child's own
                prompt never renders the CURRENT USER block, since its
                trigger_type is SUB_AGENT, not an interactive one).
        Returns the completed AgentRun with full metadata.
        """
        # A run created inside a ``tenant_scope`` must record under that tenant.
        # Falling through to the config default writes a row the connection's RLS
        # binding refuses, and the refusal arrives as an opaque
        # InsufficientPrivilege at INSERT time. See test_nested_run_tenant.py.
        resolved_tenant = tenant_id or current_tenant_scope() or self.config.tenant_id

        reason = f"Agent config not found: {agent_id}"
        if agent_config is None:
            agent_config, reason = load_agent_config_or_reason(agent_id, self.config.manifest_dir)
        if agent_config is None:
            logger.error("Agent run refused: %s", _sanitize(reason))
            session = AgentSession(agent_id, trigger_type, trigger_detail, resolved_tenant)
            session.start("", message, [])
            return session.fail(reason)

        # Resolve a concrete execution identity before creating the run.  An
        # empty role used to mean "system" and silently bypass every per-user
        # permission check.  System triggers now receive the manifest's explicit
        # service role; interactive triggers must carry a verified caller (with
        # the sole exception of explicit loopback insecure-development mode).
        effective_user_id = user_id
        effective_user_role = user_role
        if spawn_context and not effective_user_id and spawn_context.user_id:
            effective_user_id = spawn_context.user_id
            effective_user_role = spawn_context.user_role

        if trigger_type in _SYSTEM_TRIGGER_TYPES:
            effective_user_id = effective_user_id or f"service:{agent_id}"
            effective_user_role = effective_user_role or agent_config.service_role or "service"
        elif not effective_user_id or not effective_user_role:
            from robothor.auth.runtime import auth_required

            bind_host = os.environ.get("ROBOTHOR_ENGINE_HOST", "127.0.0.1")
            if not auth_required(bind_host=bind_host):
                effective_user_id = effective_user_id or "loopback-development-operator"
                effective_user_role = effective_user_role or "owner"
            else:
                logger.warning(
                    "Rejected interactive run without verified identity: agent=%s trigger=%s",
                    _sanitize(agent_id),
                    trigger_type.value,
                )
                session = AgentSession(agent_id, trigger_type, trigger_detail, resolved_tenant)
                session.start("", message, [])
                return session.fail("Authentication identity required for interactive run")

        # ── Identity — who is this run's message addressed to? ────────────
        # Precedence and its reasoning live in robothor/engine/run_identity.py:
        # explicit kwarg > webchat DB resolution > legacy Telegram `|sender:`
        # parse. A spawn_context-carried identity (sub-agent attribution only)
        # is folded in further below, after spawn inheritance is resolved.
        effective_identity = resolve_run_identity(
            identity,
            agent_id=agent_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            user_id=effective_user_id,
            user_role=effective_user_role,
            tenant_id=resolved_tenant,
            is_service_caller=_is_service_caller(effective_user_role, effective_user_id),
        )

        # Per-run reasoning effort → extended-thinking budget (task-local).
        from robothor.engine.model_registry import set_reasoning_effort

        set_reasoning_effort(agent_config.reasoning_effort)

        # Create session
        session = AgentSession(
            agent_id=agent_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            tenant_id=resolved_tenant,
            correlation_id=correlation_id,
            tool_offload_threshold=agent_config.tool_offload_threshold,
        )

        from robothor.goals.runtime import attach_run

        await asyncio.to_thread(attach_run, session.run)

        # User identity threading
        session.run.user_id = effective_user_id
        session.run.user_role = effective_user_role

        # Benchmark sandbox marker — stamps the AgentRun (read by the tool
        # wrappers) and the task-local run context (read by the memory write
        # boundary; incident 2026-09-12: a write reaching the DAL passes no tool
        # wrapper at all). Belt to the L1 allow-list suspenders in
        # robothor/engine/tools/handlers/benchmark.py. See run_context.py.
        mark_benchmark_run(session, agent_config, agent_id)

        # Sub-agent: link to parent run + inherit user identity. An empty
        # parent_run_id means the parent's own row was never recorded
        # (tracking_disabled) — insert NULL rather than a dangling FK that
        # would sink this child's entire row.
        if spawn_context:
            session.run.parent_run_id = spawn_context.parent_run_id or None
            session.run.nesting_depth = spawn_context.nesting_depth + 1
            if not session.run.user_id and spawn_context.user_id:
                session.run.user_id = spawn_context.user_id
                session.run.user_role = spawn_context.user_role
            # Contact 360 linkage — inherit parent's person.
            if spawn_context.person_id:
                session.run.person_id = spawn_context.person_id
            # Identity — inherit parent's for person_id/user_id attribution
            # only. A child's own trigger_type is SUB_AGENT, which never
            # qualifies for the CURRENT USER prompt block (see the warmup /
            # mini-preamble gating further below), so this never leaks a
            # prompt section into a worker's context — attribution only.
            if effective_identity is None and spawn_context.identity:
                effective_identity = spawn_context.identity

        # Contact 360 linkage — stamp person_id from the effective identity
        # first (covers WEBCHAT, whose trigger_detail carries no chat_id for
        # resolve_run_person_id to key off). Fall back to the existing
        # trigger_detail-based resolver when identity has no person_id.
        if not session.run.person_id and effective_identity and effective_identity.person_id:
            session.run.person_id = effective_identity.person_id

        # Contact 360 linkage — resolve from trigger_detail for top-level runs
        # whose trigger_type is telegram/chat. Best-effort; a miss is fine.
        if not session.run.person_id and trigger_type in (
            TriggerType.TELEGRAM,
            TriggerType.WEBCHAT,
        ):
            try:
                from robothor.engine.run_person_link import resolve_run_person_id

                session.run.person_id = resolve_run_person_id(
                    trigger_type=trigger_type,
                    trigger_detail=trigger_detail,
                    tenant_id=resolved_tenant,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "person_id resolution failed for %s: %s", _sanitize(agent_id), _sanitize(e)
                )

        # Stash the effective identity on the session so _run_loop can carry
        # it onto a fresh SpawnContext for any children this run spawns.
        session.identity = effective_identity

        # Resolve hierarchical tenant access.
        # owner/admin roles see child tenants; others see only their own.
        try:
            from robothor.engine.permissions import resolve_accessible_tenants

            _user_role = getattr(session.run, "user_role", None)
            session.run.accessible_tenant_ids = resolve_accessible_tenants(
                resolved_tenant, _user_role
            )
        except Exception:
            # Degrade gracefully — restrict to own tenant only.
            session.run.accessible_tenant_ids = (resolved_tenant,)

        # Build system prompt + warmup in parallel where possible.
        # Both involve sync I/O so we run them concurrently in the executor.
        loop = asyncio.get_running_loop()
        t_setup_start = time.monotonic()

        # Create stall watchdog EARLY so it covers the setup phase too.
        # Previously the watchdog was only started after setup completed,
        # meaning a hang during warmup/adapter loading went undetected.
        # Every budget from ONE derivation, scaled for the chain that serves this
        # run. A 0 budget still means "disabled" and stays 0.
        _budgets = watchdog_budgets_for(agent_config)
        stall_timeout = _budgets.stall
        effective_hard_timeout = _budgets.hard
        hard_timeout = effective_hard_timeout if effective_hard_timeout > 0 else None
        early_stall_timeout = _budgets.early_stall
        watchdog = _StallWatchdog(
            stall_timeout=stall_timeout,
            hard_timeout=effective_hard_timeout,
            early_stall_timeout=early_stall_timeout,
        )
        # Bind the watchdog to THIS task's context (see _active_watchdog_var).
        # Saved token is reset in the run-loop finally so a nested run restores
        # the parent's watchdog instead of clobbering it.
        _wd_token = _active_watchdog_var.set(watchdog)
        # Per-step bounds stop one hang; the shared total stops N compounding.
        _fin = FinalizationBudget()

        # Start watchdog immediately to cover setup phase
        _init_task = asyncio.current_task()
        if _init_task:
            watchdog.start(_init_task)
        watchdog.touch("init_begin")

        # Determine what warmup is needed (before launching parallel tasks)
        warmup_kind: str | None = None  # "cron", "interactive", or None
        if trigger_type in (TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW):
            from robothor.engine.warmup import wants_cron_warmup

            if wants_cron_warmup(agent_config):
                warmup_kind = "cron"
        elif trigger_type in (TriggerType.TELEGRAM, TriggerType.WEBCHAT):
            # Warm the first turn, then again whenever the last preamble has
            # gone stale. The old gate was `not conversation_history`, which
            # never fires on a persistent session — main.yaml sets
            # session_target: persistent and that session holds 5,500+
            # messages. Measured over 30 days: cron runs executed 11.0 warmup
            # sections each, telegram runs 0.0. The operator's own
            # conversations loaded no memory blocks, preferences or
            # breadcrumbs at all.
            #
            # The old comment said follow-ups inherit memory from conversation
            # history. They do not — the preamble is prepended to a local
            # variable and never persisted to the session.
            _since = await asyncio.to_thread(
                _seconds_since_last_interactive_run, agent_id, resolved_tenant
            )
            if should_warm_interactive(
                history_len=len(conversation_history or []), seconds_since_warmup=_since
            ):
                warmup_kind = "interactive"
        elif trigger_type == TriggerType.CHANNEL_EVENT:
            # Wake-on-surface: main reviews the channel after fleet agents
            # posted. Load the interactive preamble so main has memory blocks
            # + session continuity just like a normal chat turn.
            warmup_kind = "interactive"

        # Launch system prompt build + warmup concurrently
        sys_prompt_future = loop.run_in_executor(
            None, build_system_prompt, agent_config, self.config.workspace
        )

        warmup_future: asyncio.Future[str | None] | None = None
        if warmup_kind == "cron":
            from robothor.engine.warmup import build_warmth_preamble, set_warmup_kind

            def _build_cron_warmup() -> tuple[str, dict[str, float]] | None:
                with set_warmup_kind("cron"):
                    return build_warmth_preamble(
                        agent_config, self.config.workspace, self.config.tenant_id
                    )

            warmup_future = loop.run_in_executor(None, _build_cron_warmup)  # type: ignore[arg-type]
        elif warmup_kind == "interactive":
            from robothor.engine.warmup import (
                build_interactive_preamble,
                set_warmup_kind,
            )

            _extra_blocks = agent_config.warmup_memory_blocks or []
            _tenant = resolved_tenant

            # Extract sender name from trigger_detail (format: "chat:123|sender:Name")
            # Falls back to operator_name from config for the primary chat.
            _sender = ""
            if trigger_detail and "|sender:" in trigger_detail:
                _sender = trigger_detail.split("|sender:", 1)[1]
            elif self.config.operator_name:
                _sender = self.config.operator_name

            def _build_interactive_warmup() -> str | None:
                with set_warmup_kind("interactive"):
                    return build_interactive_preamble(
                        agent_id,
                        message,
                        include_blocks=True,
                        extra_memory_blocks=_extra_blocks,
                        tenant_id=_tenant,
                        sender_name=_sender,
                        identity=effective_identity,
                        # Host state names the configured primary from this.
                        agent_config=agent_config,
                    )

            warmup_future = loop.run_in_executor(None, _build_interactive_warmup)

        # Await both concurrently
        import uuid as _uuid  # noqa: PLC0415

        t_sys_prompt_start = time.monotonic()
        system_prompt_parts = await sys_prompt_future  # SystemPromptParts
        t_sys_prompt_ms = int((time.monotonic() - t_sys_prompt_start) * 1000)
        watchdog.touch("system_prompt_built")
        system_prompt = system_prompt_parts.full_text()  # str for mode wrapping
        # Session-goal injection moved into the warmup pipeline (build_warmth_preamble
        # / build_interactive_preamble). Owner-only scoping is enforced there so
        # workers don't see other agents' goals, and the warmup_section:session_goal
        # step shows up in agent_run_steps for telemetry.

        t_warmup_start = time.monotonic()
        warmup_preamble: str | None = None
        _warmup_section_timings: dict[str, float] = {}
        if warmup_future is not None:
            try:
                _warmup_result = await warmup_future
                # build_warmth_preamble returns (preamble, section_timings) for
                # cron warmup; build_interactive_preamble still returns str.
                if isinstance(_warmup_result, tuple):
                    warmup_preamble, _warmup_section_timings = _warmup_result
                else:
                    warmup_preamble = _warmup_result
            except Exception as e:
                logger.debug("Warmup preamble failed for %s: %s", _sanitize(agent_id), _sanitize(e))
        t_warmup_ms = int((time.monotonic() - t_warmup_start) * 1000)

        engine_preamble = warmup_preamble or ""  # its own turn; see AgentSession.start
        if not engine_preamble and (
            conversation_history
            and effective_identity is not None
            and trigger_type in (TriggerType.TELEGRAM, TriggerType.WEBCHAT)
        ):
            # Follow-up turn (warmup skipped — see warmup_kind above): the
            # first turn already got the CURRENT USER block via warmup, but
            # every turn after that needs its own reminder of who's talking,
            # since it's not re-sent as part of conversation_history. Identity
            # only — no other warmup DB work (memory blocks, entity context,
            # etc. are already in the transcript).
            try:
                from robothor.identity import enrich_identity

                # enrich_identity does blocking DB work on a cache miss —
                # offload to the executor so it never blocks the event loop,
                # mirroring the first-turn warmup path just above.
                _enriched = await loop.run_in_executor(None, enrich_identity, effective_identity)
            except Exception as e:
                logger.debug(
                    "Per-turn identity enrichment failed for %s: %s",
                    _sanitize(agent_id),
                    _sanitize(e),
                )
                _enriched = None
            try:
                engine_preamble = effective_identity.prompt_block(_enriched)
            except Exception as e:
                logger.debug(
                    "Per-turn identity block failed for %s: %s", _sanitize(agent_id), _sanitize(e)
                )
        watchdog.touch("warmup_complete")

        # ── [INJECTION] Scan the assembled system-run prompt ──
        # robothor/engine/injection_screen.py. Cron/hook/workflow runs are
        # unattended; recalled memory, skills or context files folded into the
        # prompt above could carry an injection. The screen owns the ordering
        # that makes a block auditable (terminal row inserted BEFORE the
        # guardrail event, which is an FK to it); teardown stays here, because
        # the watchdog token and _finish_run belong to the runner.
        _screen = await screen_run_prompt(
            session,
            agent_id=agent_id,
            trigger_type=trigger_type,
            system_prompt=system_prompt,
            message=f"{engine_preamble}\n\n{message}" if engine_preamble else message,
        )
        if _screen.blocked:
            # The watchdog started before setup is normally torn down by the
            # try/finally around the main run loop — but this return sits above
            # that try entirely. Without an explicit stop the watchdog is
            # orphaned: it keeps monitoring whatever task is
            # asyncio.current_task() here (the daemon's own loop task, on an
            # inline cron fire) and cancels it ~150s later, taking the whole
            # daemon down (Aug 5/9).
            watchdog.stop()
            with contextlib.suppress(Exception):
                _active_watchdog_var.reset(_wd_token)
            return self._finish_run(
                _screen.blocked_run,
                trace=None,
                agent_config=agent_config,
                session=session,
                spawn_context=spawn_context,
            )

        # ── Warmup phase instrumentation ──────────────────────────────────────
        # robothor/engine/warmup_steps.py. Warmup runs before the first
        # iteration, so a stall there shows nothing in agent_run_steps — only
        # watchdog touch logs, which nobody reads until it is already too late.
        # Section granularity is the point: "warmup took 40s" says nothing,
        # "memory_blocks took 39 of them" says everything.
        record_warmup_steps(
            session,
            prompt_ms=t_sys_prompt_ms,
            prompt_cached=bool(_prompt_cache.get(agent_config.id)),
            warmup_ms=t_warmup_ms,
            warmup_kind=warmup_kind or "",
            warmup_chars=len(warmup_preamble) if warmup_preamble else 0,
            section_timings=_warmup_section_timings,
        )

        # ── Cross-run journal resume ──────────────────────────────────────────
        # robothor/engine/journal_resume.py. Only CRON/HOOK/WORKFLOW resume:
        # an interactive run already has a human saying what it wants, and a
        # "here is where you left off" preamble would steer it back to
        # yesterday's task.
        message = maybe_prepend_journal_resume(
            message,
            agent_id=agent_id,
            agent_config=agent_config,
            trigger_type=trigger_type,
            workspace=self.config.workspace,
        )

        watchdog.touch("setup_phase_complete")
        t_setup_ms = int((time.monotonic() - t_setup_start) * 1000)
        logger.info(
            "SETUP %dms agent=%s trigger=%s warmup=%s cached_prompt=%s",
            t_setup_ms,
            _sanitize(agent_id),
            trigger_type.value,
            warmup_kind or "none",
            "hit" if _prompt_cache.get(agent_config.id) else "miss",
        )

        # ── [TOOLSET] Adapters, then this run's tools and prompt wrapping ──
        # robothor/engine/toolset_prep.py. Adapter loading is non-fatal there
        # (a dead MCP server costs its own tools, not the run), and plan mode
        # sandwiches the prompt — constraints BEFORE the identity, reminder
        # AFTER — so plan rules are not buried mid-SOUL.md.
        _prepared = await prepare_toolset(
            self.registry,
            agent_config,
            agent_id=agent_id,
            system_prompt=system_prompt,
            readonly_mode=readonly_mode,
            deep_plan=deep_plan,
        )
        tool_schemas = _prepared.tool_schemas
        tool_names = _prepared.tool_names
        system_prompt = _prepared.system_prompt
        # Deferral is otherwise invisible from inside the turn: a short schema
        # list and no statement that a longer one exists.
        engine_preamble = with_discovery_note(engine_preamble, _prepared)
        watchdog.touch("adapters_loaded")

        watchdog.touch("tools_built")
        try:
            _wp_step = RunStep(
                id=str(_uuid.uuid4()),
                run_id=session.run.id,
                step_number=0,
                step_type=StepType.WARMUP_PHASE,
                tool_name="tools_built",
                tool_input={},
                tool_output={
                    "total_setup_ms": int((time.monotonic() - t_setup_start) * 1000),
                    "tool_count": len(tool_names) if tool_names else 0,
                },
                duration_ms=int((time.monotonic() - t_setup_start) * 1000),
            )
            session.run.steps.append(_wp_step)
        except Exception as _wp_err:
            logger.debug("warmup_phase step record failed (tools_built): %s", _wp_err)

        # Execution mode: prepend enforcement preamble (full tools already loaded above)
        if execution_mode and not readonly_mode:
            system_prompt = EXECUTION_MODE_PREAMBLE + system_prompt

        # Start session
        session.start(
            system_prompt=system_prompt,
            user_message=message,
            tools_provided=tool_names,
            delivery_mode=agent_config.delivery_mode.value,
            conversation_history=conversation_history,
            engine_context=engine_preamble or None,
        )

        watchdog.touch("session_started")

        from robothor.goals.runtime import initialize_token_budget

        initialize_token_budget(session.run, agent_config, spawn_context)

        # Stage 5 — propagate the CRM task this run is advancing so the
        # agent_runs row carries it from INSERT time. Previously only the
        # auto-task path set task_id (after a separate INSERT + UPDATE),
        # leaving all sub-agent runs with NULL task_id — 0 of 44,611 rows.
        if spawn_context and spawn_context.parent_task_id:
            session.run.task_id = spawn_context.parent_task_id

        # Watchdog was created and started before setup phase (see above).
        # Stall timeout is the primary protection — kills on inactivity, not
        # elapsed wall-clock time.  Hard timeout only needed as fallback when
        # the watchdog is explicitly disabled (stall_timeout_seconds: 0).
        # This lets agents run for hours on complex tasks without being killed.
        trace = None  # initialized inside timeout block, but referenced in except handlers
        try:
            async with asyncio.timeout(hard_timeout):
                # Record run in database (sync DB call — run in executor to avoid blocking event loop)
                import psycopg2

                try:
                    await asyncio.to_thread(create_run, session.run)
                except (psycopg2.IntegrityError, psycopg2.errors.InsufficientPrivilege) as e:
                    # Deterministic rejection (CHECK/FK/unique violation, or an RLS
                    # WITH CHECK refusal when the row's tenant disagrees with the
                    # connection's binding) —
                    # retries would fail identically forever, e.g. a TriggerType
                    # enum member missing from agent_runs_trigger_type_check.
                    # Never break the run over tracking, but this is not a blip:
                    # the whole run tree would be invisible to accounting, so
                    # page the operator and stop attempting dependent writes
                    # (steps FK to the missing run row; see tracking_disabled).
                    session.run.tracking_disabled = True
                    logger.error(
                        "Run recording rejected by integrity constraint (run=%s agent=%s trigger=%s): %s",
                        session.run.id,
                        agent_config.id,
                        trigger_type.value,
                        _sanitize(e),
                    )
                    try:
                        from robothor.engine.alerts import alert as _alert
                        from robothor.engine.task_registry import get_task_registry

                        get_task_registry().spawn(
                            _alert(
                                "critical",
                                f"Run recording rejected: {agent_config.id}",
                                f"run_id={session.run.id} trigger={trigger_type.value} "
                                f"error={type(e).__name__}: {_sanitize(e)}\n"
                                "Deterministic schema rejection — this run tree is "
                                "untracked and every run of this shape will be too "
                                "until a constraint migration lands.",
                            ),
                            name=f"run-recording-alert:{agent_config.id}",
                        )
                    except Exception as alert_error:
                        logger.warning(
                            "Failed to dispatch run-recording alert: %s", _sanitize(alert_error)
                        )
                except Exception as e:
                    logger.warning("Failed to record run start: %s", _sanitize(e))

                # Auto-create CRM task if configured (skip for sub-agent runs)
                if should_create_auto_task(agent_config, spawn_context):
                    try:
                        from robothor.crm.dal import create_task as dal_create_task

                        task_id = await asyncio.to_thread(
                            lambda: dal_create_task(
                                title=f"{agent_config.name}: {trigger_type.value} run",
                                body=f"run_id: {session.run.id}\ntrigger: {trigger_detail or 'scheduled'}",
                                status="IN_PROGRESS",
                                assigned_to_agent=agent_id,
                                created_by_agent="engine",
                                priority="normal",
                                tags=[agent_id, trigger_type.value, "auto"],
                                tenant_id=self.config.tenant_id,
                            ),
                        )
                        session.run.task_id = task_id if isinstance(task_id, str) else None
                        # Persist the task_id back to the DB — create_run
                        # inserted the row before the auto-task existed, so
                        # the INSERT had NULL task_id.
                        if session.run.task_id:
                            await asyncio.to_thread(
                                lambda: update_run(session.run.id, task_id=session.run.task_id),
                            )
                    except Exception as e:
                        logger.warning("Auto-task creation failed: %s", _sanitize(e))

                # Build model list for fallback (model_override takes priority)
                if model_override:
                    models = [
                        model_override,
                        agent_config.model_primary,
                    ] + agent_config.model_fallbacks
                else:
                    models = [agent_config.model_primary] + agent_config.model_fallbacks
                models = [m for m in models if m]  # filter empty
                # Deduplicate while preserving order
                seen: set[str] = set()
                models = [m for m in models if not (m in seen or seen.add(m))]  # type: ignore[func-returns-value]

                if not models:
                    # Fallback to default model instead of hard failure
                    logger.warning(
                        "No models configured for %s; falling back to deepseek-v4-pro",
                        _sanitize(agent_id),
                    )
                    models = ["openrouter/deepseek/deepseek-v4-pro"]

                # ── [ROUTER] Classify difficulty → adjust config ──
                route = self._apply_routing(agent_config, message, len(tool_names))

                # ── [PLANNER] Generate plan if enabled ──
                plan_result = None
                plan_context = ""
                if self._should_plan(agent_config, route):
                    plan_result = await self._run_planner(
                        agent_config, message, planner_tool_names(_prepared), models
                    )
                    if plan_result and plan_result.success:
                        # Planner is non-fatal end to end: a malformed plan must
                        # never abort the run over an optional context string.
                        try:
                            from robothor.engine.planner import format_plan_context

                            plan_context = format_plan_context(plan_result)
                            if plan_context:
                                session.messages.append(
                                    {"role": ENGINE_CONTEXT_ROLE, "content": plan_context}
                                )
                        except Exception as e:
                            plan_context = ""
                            logger.warning(
                                "Plan context formatting failed (non-fatal, "
                                "continuing without plan): %s",
                                _sanitize(e),
                            )

                        # Dispatch PLAN_CREATED hook
                        try:
                            from robothor.engine.hook_registry import (
                                HookContext,
                                HookEvent,
                                get_hook_registry,
                            )

                            hr = get_hook_registry()
                            if hr:
                                await hr.dispatch(
                                    HookEvent.PLAN_CREATED,
                                    HookContext(
                                        event=HookEvent.PLAN_CREATED,
                                        agent_id=agent_config.id,
                                        run_id=session.run_id,
                                    ),
                                )
                        except Exception as e:
                            logger.warning(
                                "Failed to publish planner hook context: %s", _sanitize(e)
                            )

                # ── [TELEMETRY] Create trace context ──
                trace = self._create_trace(agent_config, session, spawn_context=spawn_context)

                # Resolve effective max_iterations (route may cap it lower, never raise it)
                max_iterations = agent_config.max_iterations
                if route and route.max_iterations_override is not None:
                    max_iterations = min(max_iterations, route.max_iterations_override)
                # Cap exploration cost in plan mode
                if readonly_mode:
                    max_iterations = min(max_iterations, 10)

                # ── [CHECKPOINT] Resume from checkpoint if requested ──
                resumed_scratchpad = None
                if resume_from_run_id:
                    resumed_scratchpad = self._resume_from_checkpoint(resume_from_run_id, session)

                # ── [SANDBOX] Create sandbox for computer-use / exec agents ──
                # Explicit "docker" always sandboxes; "host" always opts out.
                # Otherwise, sandbox-by-default applies to exec-holding agents
                # under the ROBOTHOR_SANDBOX_DEFAULT_* ladder (observe logs which
                # agents WOULD be sandboxed; enforce sandboxes them). A missing
                # image degrades to the host via the try/except below.
                from robothor.engine.feature_flags import sandbox_default_mode

                _sb_decision = _resolve_sandbox_decision(
                    agent_config, sandbox_default_mode(), agent_id=agent_id
                )
                if _sb_decision == "observe":
                    try:
                        from robothor.engine.tracking import log_guardrail_event

                        log_guardrail_event(
                            run_id=session.run.id,
                            guardrail_name="sandbox_default",
                            action="observed",
                            tool_name="exec",
                            reason="exec-holding agent would run in a Docker sandbox",
                            mode=sandbox_default_mode(),
                            step_number=0,
                        )
                    except Exception as _audit_exc:  # noqa: BLE001
                        # A control fired; losing its audit trail is itself an
                        # incident. Never let this write fail silently.
                        logger.error(
                            "guardrail event could not be recorded: %s",
                            _sanitize(_audit_exc),
                        )
                sandbox = None
                if _sb_decision == "docker":
                    from robothor.engine.sandbox import Sandbox, SandboxMode, set_current_sandbox

                    sandbox = Sandbox(
                        mode=SandboxMode.DOCKER,
                        run_id=session.run.id,
                        # Without this the container mounts nothing and every
                        # `exec` inside it lands in an empty filesystem.
                        workspace=str(self.config.workspace),
                    )
                    try:
                        await sandbox.start()
                        set_current_sandbox(sandbox)
                    except Exception as e:
                        logger.error(
                            "Sandbox start failed for %s: %s", _sanitize(agent_id), _sanitize(e)
                        )
                        sandbox = None
                        # FAIL CLOSED. Under enforce the operator has been told
                        # that exec-holding agents run contained; quietly falling
                        # back to the host would give them containment they do
                        # not have. (The old "engine user isn't in the docker
                        # group" note predates podman, which is rootless: a real
                        # container starts fine — test_sandbox_actually_starts.py.)
                        # Under observe, degrading to the host IS the contract.
                        #
                        # But the global mode is only a *default*, for agents that
                        # never expressed a preference. An agent whose manifest
                        # explicitly says `sandbox: docker` DID express one, and
                        # dropping it onto the host because some unrelated global
                        # flag says "observe" silently gives it none of the
                        # containment it asked for. Observed live: auto-agent,
                        # manifest `sandbox: docker`, container failed to start,
                        # run continued on the host, nothing surfaced.
                        _sb_explicit = agent_config.sandbox == "docker"
                        if _sb_explicit or sandbox_default_mode() == "enforce":
                            _sb_reason = f"sandbox required but could not be started: {e}"
                            try:
                                from robothor.engine.tracking import log_guardrail_event

                                log_guardrail_event(
                                    run_id=session.run.id,
                                    guardrail_name="sandbox_default",
                                    action="blocked",
                                    tool_name="exec",
                                    reason=_sb_reason,
                                    # Say which rule blocked it. Labelling an
                                    # explicit-manifest block as "enforce" would
                                    # send whoever reads this to the wrong flag.
                                    mode="explicit" if _sb_explicit else "enforce",
                                    step_number=0,
                                )
                            except Exception as _audit_exc:  # noqa: BLE001
                                logger.error(
                                    "sandbox_default blocked a run but the guardrail "
                                    "event could not be recorded: %s",
                                    _sanitize(_audit_exc),
                                )
                            return self._finish_run(
                                session.fail(f"Blocked by sandbox_default: {_sb_reason}"),
                                trace=trace,
                                agent_config=agent_config,
                                session=session,
                                spawn_context=spawn_context,
                            )

                # Watchdog already started before setup phase (see above).

                # The deferred allow-set that gates tool_call, and the toolset
                # tool_search reads. toolset_prep says why they are two things.
                _toolset_tokens = publish_toolset(self.registry, agent_config, tool_names)

                # Register the live session so external callers (Telegram /steer,
                # /chat/steer, /chat/interrupt) can influence it mid-run — the
                # loop-side consume is otherwise unreachable in production.
                # Scoped to the loop window; always unregistered in the finally.
                from robothor.engine import session_registry

                session_registry.register(session, on_status=on_status)

                try:
                    await self._run_loop(
                        session,
                        models,
                        tool_schemas,
                        agent_config,
                        on_content,
                        on_tool,
                        max_iterations=max_iterations,
                        route=route,
                        plan_result=plan_result,
                        trace=trace,
                        resumed_scratchpad=resumed_scratchpad,
                        spawn_context=spawn_context,
                        readonly_mode=readonly_mode,
                        execution_mode=execution_mode,
                        on_status=on_status,
                        on_stream_event=on_stream_event,
                    )
                    # A run the watchdog flagged that RETURNED (cooperative
                    # abort, or the loop's own wall-clock self-check) must
                    # finalize as TIMEOUT, exactly like one the cancel
                    # reached. Only the except-handler mapped this before,
                    # so a cooperative abort finalized as COMPLETED — a
                    # run killed for overrunning its budget reported
                    # success.
                    if watchdog.was_stall_timeout:
                        reason = watchdog.abort_reason or "Watchdog abort"
                        return self._finish_run(
                            session.timeout(reason=reason),
                            trace=trace,
                            agent_config=agent_config,
                            session=session,
                            spawn_context=spawn_context,
                        )
                finally:
                    with contextlib.suppress(Exception):
                        session_registry.unregister(session)
                    with contextlib.suppress(Exception):
                        withdraw_toolset(_toolset_tokens)
                    watchdog.stop()
                    with contextlib.suppress(Exception):
                        _active_watchdog_var.reset(_wd_token)
                    # Sandbox teardown, bounded: runs after watchdog.stop().
                    if sandbox:
                        await _fin.run(sandbox.stop(), "sandbox_stop")
                        from robothor.engine.sandbox import set_current_sandbox

                        set_current_sandbox(None)

        except (TimeoutError, asyncio.CancelledError) as _cancel_exc:
            # Prefer the watchdog's structured abort_reason (names the
            # last progress signal). Fall back to hard-timeout framing
            # only when cancellation came from outside the watchdog.
            abort_reason = watchdog.abort_reason or ""
            if watchdog.was_stall_timeout:
                reason = abort_reason or f"Stall watchdog: no progress for {stall_timeout}s"
                logger.warning("Agent %s killed: %s", _sanitize(agent_id), _sanitize(reason))
                session.record_error(reason)
                spawn_post_stall_autodream(agent_id)
                return self._finish_run(
                    session.timeout(reason=reason),
                    trace=trace,
                    agent_config=agent_config,
                    session=session,
                    spawn_context=spawn_context,
                )
            # Cancelled from outside (circuit breaker, daemon shutdown,
            # or a caller-level wait_for). Name what we know.
            # A WORKFLOW budget expiring mid-call is its own evidence: this
            # run's clock never fired, and the exception already names the
            # workflow, step and model. It outranks abort_reason, which would
            # otherwise both mask the message and re-stamp the row a timeout.
            _deadline = str(_cancel_exc) if isinstance(_cancel_exc, WorkflowDeadlineError) else ""
            _outcome = _cancel_outcome(
                timed_out=isinstance(_cancel_exc, TimeoutError),
                declared_timeout_seconds=agent_config.timeout_seconds,
                effective_ceiling=effective_hard_timeout,
                last_activity=watchdog.last_activity_desc,
                waiting_on=watchdog.waiting_on,
                workflow_deadline=_deadline,
            )
            reason = _deadline or abort_reason or _outcome.reason
            logger.warning("Agent %s cancelled: %s", _sanitize(agent_id), _sanitize(reason))
            session.record_error(reason)
            # Diagnostic dump for the noon-storm investigation. Captures
            # who else is alive at cancel time, the watchdog's last touch,
            # and elapsed-since-start. Lands in agent_runs.error_traceback.
            diag = _build_cancel_diagnostic(watchdog, agent_id)
            # The one finalization OUTSIDE the outer asyncio.timeout — see
            # finalization_budget's module docstring for what that cost.
            _finish = asyncio.to_thread(
                self._finish_run,
                terminal_run(session, _outcome, reason, diag, bool(abort_reason) and not _deadline),
                trace=trace,
                agent_config=agent_config,
                session=session,
                spawn_context=spawn_context,
            )
            finished = await _fin.run(_finish, "finish_after_cancel") or session.run
            # The row is written; now let the cancellation continue.
            #
            # Catching it at all is right — 29 runs sat `running` forever
            # before this handler existed. Catching it and RETURNING was the
            # other half of the bug: an outer deadline becomes a suggestion,
            # because `asyncio.timeout` only raises TimeoutError if its
            # cancellation reaches the context manager. Absorbed here, the
            # enclosing block exits normally and the cap silently does
            # nothing. Measured 2026-08-24: benchmark-runner's own 3600s
            # ceiling cancelled its task, the benchmark case inside absorbed
            # it, and the sweep ran on for three more hours — losing one
            # innocent agent's case to the same kill every hour.
            #
            # Only a cancellation this run did not cause propagates. Its own
            # watchdog (handled above) and its own hard cap (TimeoutError,
            # not CancelledError) still return a timed-out run, because for
            # those the deadline that fired was this run's to enforce.
            #
            # A workflow-budget kill propagates for the same reason with a
            # different consumer: the row above is written, and only the
            # workflow engine knows which STEP to mark. Swallowing it here is
            # what made the step land `failed` with a circuit-breaker reason.
            if propagates_to_caller(_cancel_exc):
                raise
            return finished
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("Agent %s failed: %s", _sanitize(agent_id), _sanitize(e), exc_info=True)
            session.record_error(str(e), tb)
            return self._finish_run(
                session.fail(str(e), tb),
                trace=trace,
                agent_config=agent_config,
                session=session,
                spawn_context=spawn_context,
            )

        # ── [INTERRUPT] Operator halted the run — finalize as CANCELLED ──
        # Skip the verifier and the COMPLETED finalization; the run was cut short
        # on purpose.
        if session.was_interrupted:
            return self._finish_run(
                session.cancelled(session._interrupt_note),
                trace=trace,
                agent_config=agent_config,
                session=session,
                spawn_context=spawn_context,
            )

        # ── [VERIFIER] Self-validation step ──
        output_text = session.get_final_text()
        if self._should_verify(agent_config, route, session):
            output_text = await self._run_verification(
                agent_config,
                session,
                models,
                tool_schemas,
                output_text,
                on_content,
                on_tool,
                max_iterations=max_iterations,
                route=route,
                plan_result=plan_result,
                trace=trace,
                on_status=on_status,
                on_stream_event=on_stream_event,
            )

        # ── [TELEMETRY] Publish run metrics ──
        self._publish_run_telemetry(trace, session.run)

        return self._finish_run(
            session.complete(output_text),
            trace=trace,
            agent_config=agent_config,
            session=session,
            spawn_context=spawn_context,
        )

    # ─── Deep Mode (RLM bypass) ───────────────────────────────────────

    async def execute_deep(
        self,
        query: str,
        *,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        context_override: str | None = None,
        trigger_type: TriggerType = TriggerType.MANUAL,
        tenant_id: str | None = None,
        user_id: str = "",
        user_role: str = "",
        identity: IdentityContext | None = None,
    ) -> AgentRun:
        """Execute a deep reasoning session via the RLM, bypassing the LLM loop.

        This is the engine-side implementation for /deep.  Unlike execute(),
        it calls execute_deep_reason() directly — the user explicitly requested
        the RLM, so no LLM needs to "decide" to invoke the tool.

        Args:
            query: The user's question / reasoning request.
            on_progress: Optional callback emitting {elapsed_s, status} every 5s.
            conversation_history: Recent conversation for context (not sent to RLM
                as messages — summarised as context string).
            identity: Unified identity context (``robothor.identity``). Deep
                mode has no system-prompt/warmup seam of its own, so its
                CURRENT USER block is prepended directly to the RLM context.

        Returns:
            AgentRun with output_text set to the RLM response, cost unified.
        """
        import uuid

        from robothor.engine.session import AgentSession

        agent_id = "main"
        # A run created inside a ``tenant_scope`` must record under that tenant.
        # Falling through to the config default writes a row the connection's RLS
        # binding refuses, and the refusal arrives as an opaque
        # InsufficientPrivilege at INSERT time. See test_nested_run_tenant.py.
        resolved_tenant = tenant_id or current_tenant_scope() or self.config.tenant_id
        if not user_id or not user_role:
            from robothor.auth.runtime import auth_required

            bind_host = os.environ.get("ROBOTHOR_ENGINE_HOST", "127.0.0.1")
            if not auth_required(bind_host=bind_host):
                user_id = user_id or "loopback-development-operator"
                user_role = user_role or "owner"
            else:
                session = AgentSession(
                    agent_id=agent_id,
                    trigger_type=trigger_type,
                    trigger_detail="deep_reason",
                    tenant_id=resolved_tenant,
                )
                session.start("", query, ["deep_reason"])
                return session.fail("Authentication identity required for interactive run")

        session = AgentSession(
            agent_id=agent_id,
            trigger_type=trigger_type,
            trigger_detail="deep_reason",
            tenant_id=resolved_tenant,
        )
        session.run.user_id = user_id
        session.run.user_role = user_role
        session.start(
            system_prompt="",
            user_message=query,
            tools_provided=["deep_reason"],
            delivery_mode="none",
        )

        # Record run in DB
        try:
            create_run(session.run)
        except Exception as e:
            logger.warning("Failed to record deep run start: %s", _sanitize(e))

        # Build context — use override (from deep plan) or fall back to conversation history
        if context_override:
            context = context_override
        else:
            context = ""
            if conversation_history:
                recent = conversation_history[-10:]  # Last 5 turns
                context_parts = []
                for msg in recent:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role in ("user", "assistant") and content:
                        context_parts.append(f"{role}: {content[:500]}")
                if context_parts:
                    context = "Recent conversation context:\n" + "\n".join(context_parts)

        if identity is not None:
            try:
                from robothor.identity import enrich_identity

                # enrich_identity does blocking DB work on a cache miss —
                # offload to the executor so it never blocks the event loop.
                enriched = await asyncio.get_running_loop().run_in_executor(
                    None, enrich_identity, identity
                )
            except Exception as e:
                logger.debug("Deep-mode identity enrichment failed: %s", _sanitize(e))
                enriched = None
            try:
                identity_block = identity.prompt_block(enriched)
                context = f"{identity_block}\n\n{context}" if context else identity_block
            except Exception as e:
                logger.debug("Deep-mode identity block failed: %s", _sanitize(e))

        start_time = time.monotonic()

        # Progress heartbeat: emit elapsed time every 5s while RLM runs
        progress_stop = asyncio.Event()
        # Thread-safe queue for RLM event callbacks (called from worker thread)
        import queue as _queue

        event_queue: _queue.SimpleQueue[dict[str, Any]] = _queue.SimpleQueue()
        last_event: dict[str, Any] | None = None

        async def _progress_loop() -> None:
            nonlocal last_event
            elapsed = 0
            while not progress_stop.is_set():
                await asyncio.sleep(5)
                if progress_stop.is_set():
                    break
                elapsed = int(time.monotonic() - start_time)
                # Drain event queue
                while not event_queue.empty():
                    try:
                        last_event = event_queue.get_nowait()
                    except Exception:
                        break
                if on_progress:
                    progress: dict[str, Any] = {"elapsed_s": elapsed, "status": "running"}
                    if last_event:
                        progress["last_event"] = last_event
                    with contextlib.suppress(Exception):
                        await on_progress(progress)

        progress_task = asyncio.create_task(_progress_loop())

        try:
            from robothor.engine.rlm_tool import DeepReasonConfig, execute_deep_reason

            config = DeepReasonConfig(workspace=str(self.config.workspace))
            result = await asyncio.to_thread(  # type: ignore[call-arg]
                execute_deep_reason,
                query=query,
                context=context,
                config=config,
                on_event=lambda e: event_queue.put_nowait(e),
            )

            progress_stop.set()
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

            elapsed = time.monotonic() - start_time

            if "error" in result:
                error_msg = result["error"]
                session.record_error(error_msg)

                # Record deep_reason step even on failure
                step = RunStep(
                    id=str(uuid.uuid4()),
                    run_id=session.run.id,
                    step_number=1,
                    step_type=StepType.DEEP_REASON,
                    tool_name="deep_reason",
                    tool_input={"query": query},
                    tool_output=result,
                    duration_ms=int(elapsed * 1000),
                    error_message=error_msg,
                )
                session.run.steps.append(step)

                return self._finish_run(session.fail(error_msg))

            # Success
            response_text = result.get("response", "")
            cost_usd = result.get("cost_usd", 0.0)
            execution_time_s = result.get("execution_time_s", round(elapsed, 1))
            context_chars = result.get("context_chars", 0)
            trajectory_file = result.get("trajectory_file", "")

            # Unify cost into run totals
            session.run.total_cost_usd += cost_usd

            # Record deep_reason step
            step = RunStep(
                id=str(uuid.uuid4()),
                run_id=session.run.id,
                step_number=1,
                step_type=StepType.DEEP_REASON,
                tool_name="deep_reason",
                tool_input={"query": query, "context_chars": context_chars},
                tool_output={
                    "response_chars": len(response_text),
                    "cost_usd": cost_usd,
                    "execution_time_s": execution_time_s,
                    "trajectory_file": trajectory_file,
                },
                duration_ms=int(elapsed * 1000),
            )
            session.run.steps.append(step)

            return self._finish_run(session.complete(response_text))

        except Exception as e:
            progress_stop.set()
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

            tb = traceback.format_exc()
            logger.error("execute_deep failed: %s", _sanitize(e), exc_info=True)
            session.record_error(str(e), tb)
            return self._finish_run(session.fail(str(e), tb))

    async def _run_loop(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        agent_config: AgentConfig,
        on_content: Callable[[str], Awaitable[None]] | None = None,
        on_tool: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        *,
        max_iterations: int = 20,
        route: Any = None,
        plan_result: Any = None,
        trace: Any = None,
        resumed_scratchpad: Any = None,
        spawn_context: SpawnContext | None = None,
        readonly_mode: bool = False,
        execution_mode: bool = False,
        on_status: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Core conversation loop: LLM call → tool execution → repeat."""
        # Track models that hit permanent errors (401/403/429) across iterations
        broken_models: set[str] = set()

        # Error recovery state
        _helper_spawns_used: int = 0
        _replan_count: int = 0

        # Set spawn context for sub-agent tools (via contextvars)
        if spawn_context:
            # This is a sub-agent run — use the provided context
            from robothor.engine.tools import _current_spawn_context

            _current_spawn_context.set(spawn_context)
        elif agent_config.can_spawn_agents:
            # This is a top-level run that can spawn — create fresh context
            import uuid

            from robothor.engine.tools import _current_spawn_context

            fresh_ctx = SpawnContext(
                # An untracked run (tracking_disabled) has no agent_runs row —
                # advertising its id would make every child's insert fail the
                # parent_run_id FK. Empty string → children record NULL parent.
                parent_run_id="" if session.run.tracking_disabled else session.run.id,
                parent_agent_id=agent_config.id,
                correlation_id=session.run.correlation_id or str(uuid.uuid4()),
                nesting_depth=0,
                max_nesting_depth=agent_config.max_nesting_depth,
                max_spawn_batch=agent_config.max_spawn_batch,
                remaining_token_budget=session.run.token_budget,
                parent_trace_id=trace.trace_id if trace else "",
                parent_span_id="",
                person_id=session.run.person_id,
                identity=getattr(session, "identity", None),
            )
            _current_spawn_context.set(fresh_ctx)

        # ── v2: Initialize enhancement objects ──
        scratchpad = self._create_scratchpad(agent_config, route, resumed_scratchpad)
        escalation = self._create_escalation(agent_config)
        checkpoint = self._create_checkpoint(agent_config, route, session.run_id)
        guardrail_engine = self._create_guardrails(agent_config)

        # ── v2: Initialize in-conversation todo list ──
        if agent_config.todo_list_enabled:
            from robothor.engine.todolist import TodoList

            session.todo_list = TodoList(items=[])

        # Inject guardrail awareness into system prompt so LLM self-regulates
        if guardrail_engine and guardrail_engine.enabled_policies:
            from robothor.engine.guardrails import guardrail_summary

            gr_text = guardrail_summary(guardrail_engine.enabled_policies)
            if gr_text and session.messages and session.messages[0].get("role") == "system":
                session.messages[0]["content"] += f"\n\n---\n\n{gr_text}"

        # ── v2: Lifecycle hooks ──
        from robothor.engine.hook_registry import (
            HookContext,
            HookEvent,
            get_hook_registry,
        )

        hook_registry = get_hook_registry()

        # Dispatch AGENT_START hook
        if hook_registry:
            try:
                start_ctx = HookContext(
                    event=HookEvent.AGENT_START,
                    agent_id=agent_config.id,
                    run_id=session.run.id,
                )
                await hook_registry.dispatch(HookEvent.AGENT_START, start_ctx)
            except Exception as e:
                logger.warning("AGENT_START hook error: %s", _sanitize(e))

        # Build tool sets for runtime enforcement
        _allowed_tool_set: frozenset[str] = frozenset(
            s["function"]["name"] for s in tool_schemas if "function" in s
        )
        _readonly_tool_set: frozenset[str] = frozenset()
        if readonly_mode:
            from robothor.engine.tools.constants import READONLY_TOOLS

            _readonly_tool_set = READONLY_TOOLS

        # Wire plan into scratchpad for progress tracking
        if scratchpad and plan_result and hasattr(plan_result, "plan") and plan_result.plan:
            scratchpad.set_plan(plan_result.plan)

        plan_steps = 0
        if plan_result and hasattr(plan_result, "estimated_steps"):
            plan_steps = plan_result.estimated_steps

        # Soft check-in interval (repurposed from old max_iterations hard cap)
        _checkin_interval = max_iterations
        _safety_cap = getattr(agent_config, "safety_cap", 200)
        _iteration = 0
        _pre_iteration_msg_idx = len(session.messages)
        _tool_failures: dict[str, int] = {}  # per-tool failure count for circuit breaker
        _guard_state = GuardState()  # carries the 500K alert's one-shot latch
        _mode = mode_for_run(session.run_id)  # reads once, seeds the cache
        _pacer = DeadlinePacer(mode=_mode)
        # ── [WALLCLOCK] the loop's own deadline — computed once, checked
        # every iteration. See the self-check below for why this exists.
        # ONE resolver, shared with the stall watchdog: a task-imposed budget
        # scaled to 1600 while the container was destroyed at 1500 is how a
        # graded run died mid-LLM-call (robothor/engine/run_deadline.py).
        _stop = BudgetStop.for_run(
            agent_config, mode=_mode, watchdog=self._active_watchdog, session=session
        )
        _wallclock_ceiling = _stop.budget.seconds
        _wallclock_deadline = (
            time.monotonic() + _wallclock_ceiling if _wallclock_ceiling > 0 else None
        )

        _workspace = getattr(agent_config, "workspace", "") or self.config.workspace

        while True:
            # ── [BUDGET] Wrap up at 90%, and END at 100% ──
            # BEFORE the guards below, and that order is load-bearing: the
            # wallclock self-check fires at the same instant and ends the run
            # as a TIMEOUT holding whatever the conversation had. This is the
            # graceful form of the same deadline (run_deadline.py), so it
            # looks first; the self-check is untouched at `off`/`observe`.
            _phase = _stop.due()
            if _phase == "expired":
                end_run_at_budget(
                    session,
                    _stop.budget,
                    elapsed=_stop.elapsed,
                    task_text=task_text_from(session.messages),
                    workspace=_workspace,
                )
                return
            if _phase == "wrapup" and _stop.announce("wrapup"):
                tool_schemas = wrapup_schemas(tool_schemas)
                begin_wrapup(session, _stop)  # admission enforces what the schema drops
                append_engine_note(
                    session,
                    wrapup_note(
                        elapsed=_stop.elapsed,
                        budget_seconds=_stop.budget.seconds,
                        task_text=task_text_from(session.messages),
                        workspace=_workspace,
                    ),
                    _workspace,
                )

            # ── [GUARDS] May the loop take another iteration? ──
            # wallclock -> steer -> interrupt -> watchdog -> runaway, in that
            # order, in robothor/engine/loop_guards.py. The order is
            # load-bearing (the wallclock branch TRIPS the watchdog rather than
            # returning, so execute() maps the run to TIMEOUT rather than
            # ERROR) and could not be asserted anywhere while it lived inline
            # in a 1,059-line method.
            if check_iteration_guards(
                session,
                agent_config,
                watchdog=self._active_watchdog,
                wallclock_deadline=_wallclock_deadline,
                wallclock_ceiling=_wallclock_ceiling,
                state=_guard_state,
            ):
                return

            # ── [SAFETY VALVE] Absolute iteration cap (infinite-loop protection) ──
            # safety_cap=0 is the manifest sentinel for "no cap" (main.yaml sets
            # this for heartbeat + worker per operator directive 2026-04-20). The
            # check only fires when the cap is positive.
            # ── [DEADLINE] Tell the agent while it can still act ──
            # Rungs, wording and ladder: robothor/engine/run_pacing.py. Here is
            # the only place with the live watchdog, task text and workspace.
            _dl_note = _pacer.note_for(
                self._active_watchdog,
                iteration=_iteration,
                task_text=task_text_from(session.messages),
                workspace=_workspace,
                run_id=session.run.id,
            )
            append_engine_note(session, _dl_note)

            if _safety_cap > 0 and _iteration >= _safety_cap:
                await self._force_wrapup(
                    session,
                    models,
                    tool_schemas,
                    on_content,
                    broken_models,
                    agent_config.temperature,
                    trace,
                    reason=f"Safety limit reached ({_safety_cap} iterations).",
                )
                return

            # ── [SOFT CHECK-IN] Nudge LLM to self-assess progress ──
            # Cadence and wording in robothor/engine/run_pacing.py.
            _ci_note = checkin_note(
                _iteration,
                _checkin_interval,
                _pacer.mode,
                run_id=session.run.id,
                session=session,
                task_text=task_text_from(session.messages),
                workspace=_workspace,
                fraction=_stop.fraction_spent(),
            )
            append_engine_note(session, _ci_note, _workspace)

            # ── [STATUS] Emit iteration_start lifecycle event ──
            if on_status:
                with contextlib.suppress(Exception):
                    await on_status(
                        {
                            "event": "iteration_start",
                            "iteration": _iteration + 1,
                            "checkin_interval": _checkin_interval,
                            "safety_cap": _safety_cap,
                        }
                    )

            # ── [BUDGET] Observability only ──
            # Tokens and cost are tracked on session.run for dashboards
            # and post-run analytics. Mid-run enforcement (soft nudges,
            # hard wrap-up) only fires when an operator explicitly opts
            # in via hard_budget=true. Default: no enforcement; runs
            # continue until the agent finishes its work.
            if agent_config.hard_budget and agent_config.max_cost_usd > 0:
                budget_status = session.check_budget(0, agent_config.max_cost_usd)
                if budget_status == "exhausted" and not session.run.budget_exhausted:
                    session.run.budget_exhausted = True
                    await self._force_wrapup(
                        session,
                        models,
                        tool_schemas,
                        on_content,
                        broken_models,
                        agent_config.temperature,
                        trace,
                        reason="Hard budget limit reached (explicit cost cap).",
                    )
                    return

            # ── [CONTINUOUS MODE] Periodic progress reports ──
            if (
                agent_config.continuous
                and _iteration > 0
                and agent_config.progress_report_interval > 0
                and _iteration % agent_config.progress_report_interval == 0
            ):
                await self._send_progress_report(session, agent_config, _iteration)

            # ── [CONTEXT BUDGET] Thin, then compact, before the call ──
            # Both steps live in robothor/engine/context_budget.py. It sizes
            # against the model that will actually be tried next (G2b), runs
            # every iteration, and never raises — losing compaction costs
            # money, taking the run down with it costs the work.
            await keep_context_within_budget(
                session,
                agent_config,
                iteration=_iteration,
                models=models,
                broken_models=broken_models,
                hook_registry=hook_registry,
                pre_iteration_msg_idx=_pre_iteration_msg_idx,
            )

            # ── [SCRATCHPAD] Inject working state summary ──
            if scratchpad and scratchpad.should_inject():
                summary = scratchpad.format_summary(plan_steps=plan_steps)
                session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": summary})

            # ── [TODO REMINDER] Nudge agent to update todo list ──
            if session.todo_list and session.todo_list.should_remind():
                reminder = session.todo_list.format_reminder()
                session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": reminder})

            # ── [PRE-FLIGHT] Hard budget cost projection ──
            if (
                agent_config.hard_budget
                and agent_config.max_cost_usd > 0
                and session.project_next_call_cost() + session.run.total_cost_usd
                > agent_config.max_cost_usd
            ):
                tool_schemas = []  # force text-only final response

            # ── LLM call ──
            # No per-model timeout budgets — let each model take as long as
            # it needs.  The stall watchdog (touches on every stream chunk and
            # tool completion) is the correct guard against stuck runs.
            # The litellm HTTP timeout (600 s) handles truly dead connections.
            # The one exception is the budget window: a call still in flight
            # when the budget ends loses the whole run, so it is cancelled.
            try:
                async with _stop.call_window():
                    response, model_used, elapsed_ms, msg_dict = await self._llm_call_and_record(
                        session,
                        models,
                        tool_schemas,
                        on_content,
                        broken_models,
                        agent_config.temperature,
                        trace,
                        on_stream_event=on_stream_event,
                    )
            except RunBudgetError:
                end_run_at_budget(
                    session,
                    _stop.budget,
                    elapsed=_stop.elapsed,
                    task_text=task_text_from(session.messages),
                    workspace=_workspace,
                )
                return

            if response is None:
                raise all_models_failed(session, models, broken_models)

            if not response.choices:
                session.record_error("LLM returned empty choices")
                raise RuntimeError("LLM returned empty choices")

            assistant_msg = response.choices[0].message

            # ── [EXECUTION MODE] Strip planning markers ──
            # Prevent the LLM from re-entering plan mode during execution.
            if execution_mode and assistant_msg.content and "[PLAN_READY]" in assistant_msg.content:
                assistant_msg.content = assistant_msg.content.replace("[PLAN_READY]", "").strip()
                logger.info("Stripped [PLAN_READY] marker from execution mode output")

            # ── [TODO LIST] Track turns for reminder timing ──
            if session.todo_list:
                tool_names_in_call = {tc.function.name for tc in (assistant_msg.tool_calls or [])}
                session.todo_list.record_turn(used_todo="todo_write" in tool_names_in_call)

            # Check if we're done (no tool calls)
            if not assistant_msg.tool_calls:
                # In plan mode, nudge the agent to research if it skipped tools
                # on the very first iteration (only fires once).
                if readonly_mode and _iteration == 0:
                    session.messages.append(
                        {
                            "role": ENGINE_CONTEXT_ROLE,
                            "content": (
                                "[SYSTEM] You proposed a plan without using any tools to "
                                "research first. Before finalizing, use your tools to discover "
                                "and verify. For example: `list_directory` to find files, "
                                "`read_file` to read them, `search_memory` for context. "
                                "Do NOT ask the user to look things up for you."
                            ),
                        }
                    )
                    continue

                if nudge_for_missing_deliverable(session, _workspace):  # owes an artifact
                    continue
                return

            # ── [BUDGET] The clock again, after the call, before the work ──
            # A belt as well as a brace: the window has nothing to raise if
            # the callee swallows its CancelledError (measured 2026-08-22).
            if _stop.due() == "expired":
                end_run_at_budget(
                    session,
                    _stop.budget,
                    elapsed=_stop.elapsed,
                    task_text=task_text_from(session.messages),
                    workspace=_workspace,
                )
                return

            # ── Execute tool calls ──
            # Admission (in the model's order), execution (batched, bounded)
            # and recording (in the model's order) live in tool_turn.py — one
            # cohesive unit whose only subject is this assistant message, and
            # the module the parallel-execution rule belongs in.
            iteration_errors = await self.run_tool_turn(
                ToolTurnRequest(
                    assistant_msg=assistant_msg,
                    session=session,
                    agent_config=agent_config,
                    iteration=_iteration,
                    guardrail_engine=guardrail_engine,
                    hook_registry=hook_registry,
                    scratchpad=scratchpad,
                    escalation=escalation,
                    checkpoint=checkpoint,
                    trace=trace,
                    on_tool=on_tool,
                    on_status=on_status,
                    readonly_mode=readonly_mode,
                    readonly_tool_set=_readonly_tool_set,
                    allowed_tool_set=_allowed_tool_set,
                    guard_state=_guard_state,
                    tool_failures=_tool_failures,
                )
            )

            # ── [ERROR RECOVERY] Attempt autonomous recovery before escalation ──
            # robothor/engine/error_actions.py. `applied` suppresses the error
            # feedback below: doing both tells the agent to analyse a failure
            # the platform has just handled.
            _recovery = await apply_error_recovery(
                session,
                agent_config,
                iteration_errors=iteration_errors,
                escalation=escalation,
                readonly_mode=readonly_mode,
                helper_spawns_used=_helper_spawns_used,
                spawn_helper=lambda action: self._spawn_recovery_helper(
                    agent_config=agent_config,
                    session=session,
                    action=action,
                    spawn_context=spawn_context,
                    trace=trace,
                ),
            )
            recovery_applied = _recovery.applied
            _helper_spawns_used = _recovery.helper_spawns_used

            # ── [ERROR FEEDBACK] error_actions.py, beside its suppressor ──
            inject_error_feedback(
                session,
                agent_config,
                iteration_errors=iteration_errors,
                escalation=escalation,
                recovery_applied=recovery_applied,
            )

            # ── [ESCALATION] Check thresholds ──
            if escalation:
                if escalation.should_abort():
                    await self._force_wrapup(
                        session,
                        models,
                        tool_schemas,
                        on_content,
                        broken_models,
                        agent_config.temperature,
                        trace,
                        reason=f"Too many errors ({escalation.total_errors} total). Summarize progress.",
                    )
                    return
                esc_msg = escalation.get_escalation_message()
                if esc_msg:
                    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": esc_msg})

            # ── [REPLANNING] Is the plan still the plan? (run_replan.py) ──
            _replanned = await maybe_replan(
                session,
                agent_config,
                plan_result=plan_result,
                scratchpad=scratchpad,
                escalation=escalation,
                models=models,
                replan_count=_replan_count,
                readonly_mode=readonly_mode,
                hook_registry=hook_registry,
            )
            plan_result = _replanned.plan_result
            _replan_count = _replanned.replan_count

            # ── [CHECKPOINT] Save state (checkpoint.py, beside the loader) ──
            await save_iteration(
                checkpoint,
                session,
                agent_config=agent_config,
                scratchpad=scratchpad,
                plan_result=plan_result,
                hook_registry=hook_registry,
            )

            # Update boundary for next iteration's eager compression
            _pre_iteration_msg_idx = len(session.messages)
            _iteration += 1

            # Flush this iteration's steps to the DB so a cancelled or
            # timed-out run still leaves a per-step trail.
            try:
                await asyncio.to_thread(session.flush_new_steps_sync)
            except Exception as e:
                logger.debug("Step flush failed (non-fatal): %s", _sanitize(e))

            # Rip 1 — advance the skill nudge counter every iteration
            # that actually consumed tool calls. Bare-text iterations
            # (assistant just emits content with no tool calls) don't
            # count as "work" for the purposes of the skill nudge —
            # they're chitchat, not lessons-to-capture. The check is
            # cheap and never raises in practice; suppress for safety.
            with contextlib.suppress(Exception):
                session._iters_since_skill += 1

            # Phase 0 hook: per-iteration extension point. No-op by
            # default; future rips wire counters / steer drain / etc.
            with contextlib.suppress(Exception):
                await self._after_iteration(session, _iteration)

    # ─── Continuous mode progress report ─────────────────────────────
