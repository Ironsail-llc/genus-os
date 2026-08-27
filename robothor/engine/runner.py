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
import json
import logging
import os
import re
import time
import traceback
from typing import TYPE_CHECKING, Any

import litellm

from robothor.db.connection import current_tenant_scope
from robothor.engine.cancel_outcome import _cancel_outcome, terminal_run
from robothor.engine.config import (
    EngineConfig,
    _prompt_cache,
    build_system_prompt,
    load_agent_config,
)

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.context_budget import keep_context_within_budget

# Re-exported for existing importers. The `as` form is what marks a name as
# deliberately re-exported; a plain import reads to mypy as a private detail,
# which is the right default and the wrong one here.
from robothor.engine.deliverables import deadline_note, task_text_from  # noqa: E402
from robothor.engine.finalization_budget import FinalizationBudget  # noqa: E402

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.llm_client import LLMClient  # noqa: E402
from robothor.engine.loop_guards import GuardState, check_iteration_guards
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
from robothor.engine.run_budget import chain_for, effective_wallclock_ceiling, watchdog_budgets_for
from robothor.engine.run_budget import (
    deadline_warning as deadline_warning,
)
from robothor.engine.run_budget import (
    proactive_compaction_threshold as proactive_compaction_threshold,
)
from robothor.engine.run_finalizer import RunFinalizationMixin
from robothor.engine.run_lifecycle import RunLifecycleMixin
from robothor.engine.run_llm_calls import LLMCallMixin  # noqa: E402
from robothor.engine.sandbox_policy import agent_holds_exec, resolve_sandbox_decision
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.session import ENGINE_CONTEXT_ROLE, AgentSession
from robothor.engine.stall_watchdog import (
    _active_watchdog_var,
    _build_cancel_diagnostic,
    _StallWatchdog,
)
from robothor.engine.tool_admission import ToolAdmissionMixin  # noqa: E402
from robothor.engine.tool_outcome import record_tool_outcome
from robothor.engine.tools import get_registry
from robothor.engine.toolset_prep import prepare_toolset
from robothor.engine.tracking import create_run, update_run

#: Tools whose work is several sub-agent runs, so the agent-level per-tool cap
#: (120s by default) is far too short. Kept at a 600s floor.
_LONG_RUNNING_TOOLS = frozenset(
    {
        "benchmark_run",
        "benchmark_run_fleet",
        "benchmark_run_for_agent",
        "benchmark_compare",
        "experiment_measure",
        "spawn_agent",
        "spawn_agents",
        # Measured 2026-08-22 against 14 days of real calls: each of these died
        # at exactly the 120s default and NONE ever completed above it.
        # buddy_review_pass 8 of 10 (main had no buddy review since 08-19,
        # vision-monitor since 08-17), deep_reason 4 of 18, look 3 of 70.
        # detectors.find_tools_capped_at_timeout reports the next one.
        "buddy_review_pass",
        "deep_reason",
        "look",
    }
)

#: Of those, the tools that already enforce their OWN per-task budget: the
#: benchmark harness caps each case at the suite's ``timeout_seconds:`` (or 900s
#: by default) and records an overrun as a timeout rather than a grade. A second,
#: smaller cap out here can only cut a case short *below* the budget its suite
#: declared, and the run is then filed against the agent.
#:
#: This list previously named ``benchmark_run`` only, while the tools the fleet
#: grader actually calls are ``benchmark_run_fleet`` and
#: ``benchmark_run_for_agent`` -- so the two tools that run every benchmark
#: inherited the 120s default. Measured 2026-08-22: agent-architect
#: ``fleet-analysis`` had never once completed above 120.0s across 91 completed
#: runs, against a 512s production mean with zero production timeouts.
_HARNESS_BUDGETED_TOOLS = frozenset(
    {
        "benchmark_run",
        "benchmark_run_fleet",
        "benchmark_run_for_agent",
    }
)


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


def _resolve_tool_timeout(tool_name: str, configured: int) -> int:
    """Per-tool wall-clock cap, in seconds. 0 means unlimited.

    One owner per budget: where the callee already bounds its own work, this
    layer must not impose a second, smaller bound.
    """
    if tool_name in _HARNESS_BUDGETED_TOOLS:
        return 0
    if tool_name in _LONG_RUNNING_TOOLS:
        return max(configured, 600)
    return configured


def _normalize_model_id(model: str) -> str:
    """Collapse a model id to a provider/format-agnostic core for comparison.

    litellm reports `response.model` without the `openrouter/` prefix and often
    with a trailing date or dashes-for-dots, so an exact string compare against
    the manifest's `model_primary` would false-positive on a *healthy* run. We
    take the last path segment, drop a trailing date, and strip separators so
    `openrouter/anthropic/claude-opus-4.7` and `claude-opus-4-7-20260416`
    compare equal while still distinguishing genuinely different models.
    """
    core = (model or "").strip().lower().rsplit("/", 1)[-1]
    core = re.sub(r"[-_]?\d{6,}$", "", core)  # trailing date/build stamp
    return re.sub(r"[.\-_\s]", "", core)


# Announce-mode runs that end with fewer characters than this are flagged
# as "partial" — almost always a meta-confirmation ("briefing delivered")
# rather than the real content the agent was supposed to broadcast.
ANNOUNCE_MIN_OUTPUT_CHARS = 200

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
    from robothor.engine.task_registry import get_task_registry

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

        # Load agent config from manifest if not provided
        if agent_config is None:
            agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if agent_config is None:
            logger.error("Agent config not found: %s", _sanitize(agent_id))
            session = AgentSession(agent_id, trigger_type, trigger_detail, resolved_tenant)
            session.start("", message, [])
            return session.fail(f"Agent config not found: {agent_id}")

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
        # Precedence: explicit `identity` kwarg > WEBCHAT DB resolution >
        # legacy Telegram `|sender:` trigger_detail parse (back-compat until
        # every caller passes `identity=` explicitly). A spawn_context-carried
        # identity (sub-agent attribution only) is folded in further below,
        # after spawn inheritance is resolved.
        effective_identity = identity
        if effective_identity is None:
            if trigger_type == TriggerType.WEBCHAT and not _is_service_caller(
                effective_user_role, effective_user_id
            ):
                from robothor.identity import resolve_identity

                effective_identity = resolve_identity("webchat", effective_user_id, resolved_tenant)
            elif (
                trigger_type == TriggerType.TELEGRAM
                and trigger_detail
                and "|sender:" in trigger_detail
            ):
                from robothor.identity import IdentityContext as _IdentityContext

                _legacy_sender = trigger_detail.split("|sender:", 1)[1]
                effective_identity = _IdentityContext(
                    tenant_id=resolved_tenant,
                    channel="telegram",
                    identifier=effective_user_id,
                    verified=bool(effective_user_id and effective_user_role),
                    display_name=_legacy_sender,
                    role=effective_user_role or "",
                )
                logger.debug(
                    "execute: using legacy sender parse fallback for identity (agent=%s)",
                    _sanitize(agent_id),
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

        # User identity threading
        session.run.user_id = effective_user_id
        session.run.user_role = effective_user_role

        # Benchmark sandbox marker — when the parent (typically benchmark-runner
        # via _benchmark_run) stamps the child_config with is_benchmark=True,
        # propagate onto the AgentRun so side-effect tool wrappers (gws CLI
        # bypass, etc.) can short-circuit. Belt to the L1 allow-list
        # suspenders in robothor/engine/tools/handlers/benchmark.py.
        session.run.is_benchmark = bool(getattr(agent_config, "is_benchmark", False))

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
            has_warmup = (
                agent_config.warmup_memory_blocks
                or agent_config.warmup_context_files
                or agent_config.warmup_peer_agents
            )
            if has_warmup:
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

        if warmup_preamble:
            message = f"{warmup_preamble}\n\n{message}"
        elif (
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
                identity_block = effective_identity.prompt_block(_enriched)
                message = f"{identity_block}\n\n{message}"
            except Exception as e:
                logger.debug(
                    "Per-turn identity block failed for %s: %s", _sanitize(agent_id), _sanitize(e)
                )
        watchdog.touch("warmup_complete")

        # ── [INJECTION] Scan the assembled system-run prompt ──
        # Cron/hook/workflow runs are unattended; recalled memory, skills, or
        # context files folded into the prompt above could carry an injection.
        # Gated by ROBOTHOR_INJECTION_SCAN_* (observe logs; enforce aborts).
        if trigger_type in (
            TriggerType.CRON,
            TriggerType.HOOK,
            TriggerType.WORKFLOW,
        ):
            from robothor.engine.cron_safety import (
                CronPromptInjectionBlockedError,
                screen_cron_prompt,
            )

            try:
                _inj_finding = screen_cron_prompt(
                    f"{system_prompt}\n{message}", context=f"{trigger_type.value}:{agent_id}"
                )
            except CronPromptInjectionBlockedError as _inj_exc:
                # A blocked run must leave a complete trail. Ordering matters:
                #   1. mark the run FAILED, then INSERT it — _finish_run's
                #      persistence is a *background* task, which a short-lived
                #      caller (CLI) exits before it lands, stranding the row in
                #      'pending'. Inserting the already-terminal state is the
                #      one write guaranteed to survive.
                #   2. log the guardrail event only after the row exists —
                #      agent_guardrail_events.run_id is an FK to agent_runs, so
                #      logging first violates it and the audit event is lost.
                # Both were live defects: enforce-mode blocks were invisible to
                # the soak report and left 'pending' runs behind.
                _blocked_run = session.fail(f"Blocked by injection scan: {_inj_exc}")
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(create_run, _blocked_run)
                try:
                    from robothor.engine.tracking import log_guardrail_event

                    log_guardrail_event(
                        run_id=_blocked_run.id,
                        guardrail_name="injection_scan",
                        action="blocked",
                        tool_name=None,
                        reason=str(_inj_exc),
                        mode="enforce",
                        step_number=0,
                    )
                except Exception as _audit_exc:
                    # A security control fired; losing its audit trail is itself
                    # an incident. Never swallow this silently.
                    logger.error(
                        "injection_scan blocked run %s but the guardrail event "
                        "could not be recorded: %s",
                        _sanitize(_blocked_run.id),
                        _sanitize(_audit_exc),
                    )
                # The watchdog started before setup (above) is normally torn
                # down by the try/finally around the main run loop — but this
                # return sits above that try entirely. Without an explicit
                # stop here the watchdog is orphaned: it keeps monitoring
                # whatever task is asyncio.current_task() at this point (the
                # daemon's own loop task, on an inline cron fire) and cancels
                # it ~150s later, taking the whole daemon down (Aug 5/9).
                watchdog.stop()
                with contextlib.suppress(Exception):
                    _active_watchdog_var.reset(_wd_token)
                return self._finish_run(
                    _blocked_run,
                    trace=None,
                    agent_config=agent_config,
                    session=session,
                    spawn_context=spawn_context,
                )
            if _inj_finding:
                try:
                    from robothor.engine.tracking import log_guardrail_event

                    log_guardrail_event(
                        run_id=session.run.id,
                        guardrail_name="injection_scan",
                        action="observed",
                        tool_name=None,
                        reason=_inj_finding,
                        mode="observe",
                        step_number=0,
                    )
                except Exception as _audit_exc:  # noqa: BLE001
                    # A control fired; losing its audit trail is itself an
                    # incident. Never let this write fail silently.
                    logger.error(
                        "guardrail event could not be recorded: %s",
                        _sanitize(_audit_exc),
                    )

        # ── Warmup phase instrumentation ──────────────────────────────────────
        # Record setup milestones as warmup_phase steps so stalls are visible
        # in agent_run_steps instead of only in watchdog touch logs.
        # Per-section timings from build_warmth_preamble let us pinpoint
        # exactly which warmup section (history, memory_blocks, context_files,
        # peers, breadcrumbs, preferences, agent_hooks) stalled — crucial for
        # diagnosing fleet-wide warmup stalls (FIX-WARMUP-STALL task).
        _warmup_phase_steps: list[tuple[str, int, dict[str, Any]]] = [
            (
                "system_prompt_build",
                t_sys_prompt_ms,
                {"cached": "hit" if _prompt_cache.get(agent_config.id) else "miss"},
            ),
            (
                "warmup_preamble_build",
                t_warmup_ms,
                {
                    "kind": warmup_kind or "none",
                    "chars": len(warmup_preamble) if warmup_preamble else 0,
                },
            ),
        ]
        # Inject per-section timings as individual warmup_phase steps so we
        # can pinpoint stalls at section granularity, not just total warmup ms.
        for _sec_name, _sec_elapsed in _warmup_section_timings.items():
            _warmup_phase_steps.append(
                (
                    f"warmup_section:{_sec_name}",
                    int(_sec_elapsed * 1000),
                    {"section": _sec_name, "slow": _sec_elapsed > 0.5},
                )
            )
        for _wp_name, _wp_ms, _wp_meta in _warmup_phase_steps:
            try:
                _wp_step = RunStep(
                    id=str(_uuid.uuid4()),
                    run_id=session.run.id,
                    step_number=0,  # pre-iteration; grader ignores step_number for warmup_phase
                    step_type=StepType.WARMUP_PHASE,
                    tool_name=_wp_name,
                    tool_input={},
                    tool_output=_wp_meta,
                    duration_ms=_wp_ms,
                )
                session.run.steps.append(_wp_step)
            except Exception as _wp_err:
                logger.debug("warmup_phase step record failed (%s): %s", _wp_name, _wp_err)

        # ── Cross-run journal resume ──────────────────────────────────────────
        # If the agent has resume_on_start=true and a journal_file configured,
        # load the journal and inject it as a preamble to the message so the
        # agent wakes up knowing exactly where it left off.
        if (
            trigger_type in (TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW)
            and agent_config.resume_on_start
            and agent_config.journal_file
        ):
            try:
                from robothor.engine.journal import JournalManager

                journal_state = JournalManager.load(
                    agent_id, agent_config.journal_file, self.config.workspace
                )
                if journal_state:
                    journal_preamble = JournalManager.format_resume_preamble(journal_state)
                    message = f"{journal_preamble}\n\n{message}"
                    logger.info(
                        "Journal resume injected for %s: experiment=%s iteration=%d next_action=%s",
                        _sanitize(agent_id),
                        _sanitize(journal_state.experiment_id),
                        journal_state.iteration,
                        _sanitize(journal_state.next_action),
                    )
            except Exception as e:
                logger.warning(
                    "Journal resume failed for %s (non-fatal): %s",
                    _sanitize(agent_id),
                    _sanitize(e),
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
        )

        watchdog.touch("session_started")

        # Auto-derive token budget for TRACKING ONLY (not enforced as a hard limit)
        from robothor.engine.model_registry import compute_token_budget

        auto_budget = compute_token_budget(agent_config.model_primary, agent_config.max_iterations)
        session.run.token_budget = auto_budget

        # Sub-agent: cascade parent's remaining token budget (child can never exceed parent)
        if spawn_context and spawn_context.remaining_token_budget > 0:
            if auto_budget > 0:
                session.run.token_budget = min(auto_budget, spawn_context.remaining_token_budget)
            else:
                session.run.token_budget = spawn_context.remaining_token_budget

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
                    plan_result = await self._run_planner(agent_config, message, tool_names, models)
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

                # Deferred tools (Rip 16 / G4): when this agent's toolset is
                # deferred, tool_schemas above were reduced to core+meta. Record
                # the agent's full allowed set so the tool_call meta-tool can
                # reach (only) allowed tools on demand. No-op when deferral off.
                _defer_token = None
                if self.registry.should_defer(agent_config):
                    from robothor.engine.tools.dispatch import set_deferred_allowed

                    _defer_token = set_deferred_allowed(
                        self.registry.deferred_whitelist(agent_config)
                    )

                # Register the live session so external callers (Telegram /steer,
                # /chat/steer, /chat/interrupt) can influence it mid-run — the
                # loop-side consume is otherwise unreachable in production.
                # Scoped to the loop window; always unregistered in the finally.
                from robothor.engine import session_registry

                session_registry.register(session)

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
                    if _defer_token is not None:
                        from robothor.engine.tools.dispatch import clear_deferred_allowed

                        with contextlib.suppress(Exception):
                            clear_deferred_allowed(_defer_token)
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
                # Trigger autoDream consolidation as post-stall cleanup
                try:
                    from robothor.engine.autodream import is_cooled_down, run_autodream

                    if is_cooled_down():
                        from robothor.engine.task_registry import get_task_registry

                        get_task_registry().spawn(
                            run_autodream(mode="post_stall"),
                            name=f"autodream-post-stall:{agent_id}",
                        )
                except Exception as e:
                    logger.warning("autoDream post_stall failed: %s", _sanitize(e))
                return self._finish_run(
                    session.timeout(reason=reason),
                    trace=trace,
                    agent_config=agent_config,
                    session=session,
                    spawn_context=spawn_context,
                )
            # Cancelled from outside (circuit breaker, daemon shutdown,
            # or a caller-level wait_for). Name what we know.
            _outcome = _cancel_outcome(
                timed_out=isinstance(_cancel_exc, TimeoutError),
                declared_timeout_seconds=agent_config.timeout_seconds,
                effective_ceiling=effective_hard_timeout,
                last_activity=watchdog.last_activity_desc,
                waiting_on=watchdog.waiting_on,
            )
            reason = abort_reason or _outcome.reason
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
                terminal_run(session, _outcome, reason, diag, bool(abort_reason)),
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
            if isinstance(_cancel_exc, asyncio.CancelledError):
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
        _secret_notified = False  # one-shot latch for the credential-exposure note
        _deadline_warned = False  # one-shot latch for the wrap-up note
        # ── [WALLCLOCK] the loop's own deadline — computed once, checked
        # every iteration. See the self-check below for why this exists.
        _wallclock_ceiling = effective_wallclock_ceiling(
            agent_config.timeout_seconds, chain_for(agent_config)
        )
        _wallclock_deadline = (
            time.monotonic() + _wallclock_ceiling if _wallclock_ceiling > 0 else None
        )

        while True:
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
            # A run killed at its ceiling loses whatever it had not yet
            # written. Warning once at 80% lets it flush partial results —
            # which is the difference between partial credit and none.
            if not _deadline_warned and self._active_watchdog is not None:
                _dl_note = deadline_note(
                    self._active_watchdog.elapsed_seconds,
                    float(getattr(self._active_watchdog, "_hard_timeout", 0) or 0),
                    task_text_from(session.messages),
                    getattr(agent_config, "workspace", "") or self.config.workspace,
                )
                if _dl_note:
                    _deadline_warned = True
                    logger.info("Deadline warning issued at iteration %d", _iteration)
                    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": _dl_note})

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
            if _iteration > 0 and _checkin_interval > 0 and _iteration % _checkin_interval == 0:
                session.messages.append(
                    {
                        "role": ENGINE_CONTEXT_ROLE,
                        "content": (
                            f"[SYSTEM] Progress check-in (iteration {_iteration}): "
                            "Are you making progress toward the goal? If you are stuck "
                            "in a loop or have completed the task, provide your final "
                            "answer and stop calling tools. If making progress, continue."
                        ),
                    }
                )

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

            if response is None:
                session.record_error("All models failed")
                raise RuntimeError("All models failed to respond")

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
                return

            # ── Execute tool calls ──
            iteration_errors: list[tuple[str, str, Any]] = []

            # ── [STATUS] Emit tools_start lifecycle event ──
            if on_status:
                with contextlib.suppress(Exception):
                    tool_names_list = [tc.function.name for tc in assistant_msg.tool_calls]
                    await on_status(
                        {
                            "event": "tools_start",
                            "tools": tool_names_list,
                            "count": len(tool_names_list),
                            "iteration": _iteration + 1,
                        }
                    )

            for tc in assistant_msg.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                # ── [ADMISSION] Every gate between the ask and the call ──
                # Order is a security property; see tool_admission.py.
                verdict = await self._admit_tool_call(
                    tc=tc,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    session=session,
                    agent_config=agent_config,
                    guardrail_engine=guardrail_engine,
                    hook_registry=hook_registry,
                    readonly_mode=readonly_mode,
                    readonly_tool_set=_readonly_tool_set,
                    allowed_tool_set=_allowed_tool_set,
                )
                # A MODIFY hook may have rewritten the arguments; the call
                # below must use what admission returned, never its own copy.
                tool_args = verdict.tool_args
                if not verdict.allowed:
                    self._record_refusal(
                        verdict,
                        tc=tc,
                        tool_name=tool_name,
                        session=session,
                        scratchpad=scratchpad,
                        escalation=escalation,
                        iteration_errors=iteration_errors,
                    )
                    continue

                # Emit tool_start event
                if on_tool:
                    with contextlib.suppress(Exception):
                        await on_tool(
                            {
                                "event": "tool_start",
                                "tool": tool_name,
                                "args": tool_args,
                                "call_id": tc.id,
                            }
                        )

                # ── [TELEMETRY] Tool span ──
                tool_start = time.monotonic()
                _tool_timeout = _resolve_tool_timeout(
                    tool_name, getattr(agent_config, "tool_timeout_seconds", 120)
                )
                if trace:
                    with trace.span("tool_call", tool=tool_name) as _span:
                        result = await self.registry.execute(
                            tool_name,
                            tool_args,
                            agent_id=agent_config.id,
                            run_id=session.run.id,
                            tenant_id=session.run.tenant_id,
                            workspace=str(self.config.workspace),
                            user_id=session.run.user_id,
                            user_role=session.run.user_role,
                            timeout=_tool_timeout,
                            accessible_tenant_ids=session.run.accessible_tenant_ids,
                            task_author_override=agent_config.task_author_override,
                            is_benchmark=session.run.is_benchmark,
                            identity=getattr(session, "identity", None),
                        )
                else:
                    result = await self.registry.execute(
                        tool_name,
                        tool_args,
                        agent_id=agent_config.id,
                        run_id=session.run.id,
                        tenant_id=session.run.tenant_id,
                        workspace=str(self.config.workspace),
                        user_id=session.run.user_id,
                        user_role=session.run.user_role,
                        timeout=_tool_timeout,
                        accessible_tenant_ids=session.run.accessible_tenant_ids,
                        task_author_override=agent_config.task_author_override,
                        is_benchmark=session.run.is_benchmark,
                        identity=getattr(session, "identity", None),
                    )
                tool_elapsed = int((time.monotonic() - tool_start) * 1000)

                error_msg: str | None = result.get("error") if isinstance(result, dict) else None

                # ── [GUARDRAILS] Post-execution check ──
                if guardrail_engine and not error_msg:
                    post_gr = guardrail_engine.check_post_execution(tool_name, result)
                    if post_gr.action == "warned":
                        logger.warning("Guardrail warning for %s: %s", tool_name, post_gr.reason)
                        # And tell the AGENT. This used to be the log line
                        # alone, which meant the platform could detect a
                        # credential in a file the agent had just read and the
                        # agent would never know: it carried on, published it,
                        # and never warned the user. Detection that reaches no
                        # one is the same shape as a control that never runs.
                        #
                        # Once per run, not per tool call — repeating it every
                        # iteration would crowd out the task. The reason string
                        # names the KIND of credential and never the value,
                        # because this message is persisted with the
                        # conversation.
                        # Redact before the result reaches the model. The
                        # agent needs to know a credential is THERE — which
                        # file, what kind — and never needs the characters.
                        # Without this it quotes the key back while correctly
                        # explaining why the key is dangerous, which leaks it
                        # into the transcript, the session store, and every
                        # log downstream of them.
                        if post_gr.guardrail_name == "no_sensitive_data":
                            from robothor.engine.guardrails import redact_secrets

                            result = redact_secrets(result)

                        if post_gr.guardrail_name == "no_sensitive_data" and not _secret_notified:
                            _secret_notified = True
                            session.messages.append(
                                {
                                    "role": ENGINE_CONTEXT_ROLE,
                                    "content": (
                                        f"[SYSTEM] {post_gr.reason} Treat this as a "
                                        "credential exposure: tell the user which file "
                                        "or output contains it — WITHOUT repeating the "
                                        "value — and that it should be removed from the "
                                        "code and rotated. Do not commit, push, send, or "
                                        "otherwise publish content containing it."
                                    ),
                                }
                            )

                # ── [HOOKS] Post-tool-use lifecycle hook ──
                if hook_registry:
                    try:
                        post_tool_ctx = HookContext(
                            event=HookEvent.POST_TOOL_USE,
                            agent_id=agent_config.id,
                            run_id=session.run.id,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            tool_result=result,
                        )
                        await hook_registry.dispatch(HookEvent.POST_TOOL_USE, post_tool_ctx)
                    except Exception as e:
                        logger.warning(
                            "POST_TOOL_USE hook error for %s: %s",
                            _sanitize(tool_name),
                            _sanitize(e),
                        )

                # ── [COST] Propagate tool-reported costs (e.g., deep_reason RLM) ──
                if isinstance(result, dict) and not error_msg:
                    tool_cost = result.get("cost_usd")
                    if tool_cost and isinstance(tool_cost, (int, float)) and tool_cost > 0:
                        session.run.total_cost_usd += tool_cost

                # Emit tool_end event
                if on_tool:
                    try:
                        result_preview = json.dumps(result, default=str)
                        if len(result_preview) > 2000:
                            result_preview = result_preview[:2000] + "..."
                    except Exception as e:
                        logger.warning("JSON serialization of tool result failed: %s", _sanitize(e))
                        result_preview = str(result)[:2000]
                    with contextlib.suppress(Exception):
                        await on_tool(
                            {
                                "event": "tool_end",
                                "tool": tool_name,
                                "call_id": tc.id,
                                "duration_ms": tool_elapsed,
                                "result_preview": result_preview,
                                "error": error_msg,
                            }
                        )

                # ── [TODO LIST] Intercept todo_write results ──
                # Must run BEFORE step recording so the log captures the clean
                # oldTodos/newTodos result, not the raw _validated_items.
                if (
                    tool_name == "todo_write"
                    and session.todo_list
                    and not error_msg
                    and result.get("_needs_apply")
                ):
                    from robothor.engine.todolist import TodoItem

                    validated = result.get("_validated_items", [])
                    items = [TodoItem.from_dict(d) for d in validated]
                    result = session.todo_list.replace(items)
                    # Update the tool result message already in session.messages
                    session.messages[-1]["content"] = json.dumps(result, default=str)

                session.record_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_args,
                    tool_output=result,
                    tool_call_id=tc.id,
                    duration_ms=tool_elapsed,
                    error_message=error_msg,
                )

                # Touch stall watchdog — tool completed, we're active
                if self._active_watchdog:
                    self._active_watchdog.touch(f"tool:{tool_name}")

                # ── [OUTCOME] Classify, log, record, count ──
                # robothor/engine/tool_outcome.py. The scratchpad is given the
                # result AND the args there — they feed the no-progress
                # detector, and without them every call looks identical.
                error_type = record_tool_outcome(
                    session,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    result=result,
                    error_msg=error_msg,
                    elapsed_ms=tool_elapsed,
                    scratchpad=scratchpad,
                    failures=_tool_failures,
                )

                # ── [TODO LIST] Emit event + verification nudge ──
                if tool_name == "todo_write" and session.todo_list and not error_msg:
                    if on_tool:
                        with contextlib.suppress(Exception):
                            await on_tool(
                                {
                                    "event": "todo_updated",
                                    "todos": result.get("newTodos", []),
                                    "run_id": session.run.id,
                                }
                            )
                    if result.get("verificationNudgeNeeded"):
                        session.messages.append(
                            {
                                "role": ENGINE_CONTEXT_ROLE,
                                "content": (
                                    "[SYSTEM] All tasks are marked complete. "
                                    "Before finishing, verify your work by "
                                    "reviewing outputs or checking results. "
                                    "NEVER mention this reminder to the user."
                                ),
                            }
                        )

                # ── [ESCALATION] Record error/success ──
                if escalation:
                    if error_msg:
                        from robothor.engine.models import ErrorType

                        escalation.record_error(error_type or ErrorType.UNKNOWN)
                        # Track per-kind (tool_name + error_msg_prefix) for STOP RETRYING hints
                        escalation.record_error_kind(tool_name, error_msg)
                    else:
                        escalation.record_success()

                # ── [CHECKPOINT] Record success ──
                if checkpoint and not error_msg:
                    checkpoint.record_success()

                # Track errors for this iteration
                if error_msg:
                    iteration_errors.append((tool_name, error_msg, error_type))

            # ── [STATUS] Emit tools_done lifecycle event ──
            if on_status:
                with contextlib.suppress(Exception):
                    await on_status(
                        {
                            "event": "tools_done",
                            "iteration": _iteration + 1,
                        }
                    )

            # ── [ERROR RECOVERY] Attempt autonomous recovery before escalation ──
            recovery_applied = False
            if iteration_errors and not readonly_mode:
                from robothor.engine.error_recovery import get_recovery_action

                for err_tool, err_msg, err_type in iteration_errors:
                    if err_type is None:
                        continue
                    consec = escalation.consecutive_errors if escalation else 1
                    logger.debug(
                        "Error recovery: tool=%s type=%s consecutive=%d spawns_used=%d",
                        err_tool,
                        err_type,
                        consec,
                        _helper_spawns_used,
                    )
                    action = get_recovery_action(
                        error_type=err_type,
                        consecutive_count=consec,
                        agent_config=agent_config,
                        tool_name=err_tool,
                        error_msg=err_msg,
                        helper_spawns_used=_helper_spawns_used,
                    )
                    if action is None:
                        continue
                    logger.debug("Error recovery: action=%s for %s", action.action, err_tool)

                    if action.action == "backoff":
                        await asyncio.sleep(action.delay_seconds)
                        session.messages.append(
                            {
                                "role": ENGINE_CONTEXT_ROLE,
                                "content": f"[SYSTEM] {action.message} Retrying now.",
                            }
                        )
                        recovery_applied = True

                    elif action.action == "retry":
                        session.messages.append(
                            {
                                "role": ENGINE_CONTEXT_ROLE,
                                "content": f"[SYSTEM] {action.message}",
                            }
                        )
                        recovery_applied = True

                    elif action.action == "spawn" and agent_config.can_spawn_agents:
                        logger.debug("Error recovery: spawning helper for %s", err_tool)
                        helper_result = await self._spawn_recovery_helper(
                            agent_config=agent_config,
                            session=session,
                            action=action,
                            spawn_context=spawn_context,
                            trace=trace,
                        )
                        if helper_result:
                            _helper_spawns_used += 1
                            session.messages.append(
                                {
                                    "role": ENGINE_CONTEXT_ROLE,
                                    "content": (
                                        f"[ERROR RECOVERY — Helper agent result]\n"
                                        f"{helper_result}\n\n"
                                        "Use this information to adjust your approach."
                                    ),
                                }
                            )
                            recovery_applied = True

                    elif action.action == "inject":
                        session.messages.append(
                            {
                                "role": ENGINE_CONTEXT_ROLE,
                                "content": f"[SYSTEM — Recovery guidance] {action.message}",
                            }
                        )
                        recovery_applied = True

            # ── [ERROR FEEDBACK] Inject analysis prompt on errors ──
            if iteration_errors and agent_config.error_feedback and not recovery_applied:
                error_lines = "\n".join(
                    f"- {name}: {msg}" for name, msg, _etype in iteration_errors
                )
                # Inject STOP RETRYING hints for error types repeated >= 2 times
                stop_hints = escalation.get_repeated_error_hints(threshold=2) if escalation else []
                stop_hints_text = ("\n\n" + "\n".join(stop_hints)) if stop_hints else ""
                session.messages.append(
                    {
                        "role": ENGINE_CONTEXT_ROLE,
                        "content": (
                            f"[SYSTEM] The following tool calls failed:\n{error_lines}\n\n"
                            "Analyze why these failed. Consider:\n"
                            "1. Were the arguments correct?\n"
                            "2. Is there an alternative approach or different tool?\n"
                            "3. Should you skip this step and continue?\n"
                            "Do NOT retry the exact same call with the same arguments."
                            f"{stop_hints_text}"
                        ),
                    }
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

            # ── [REPLANNING] Check if mid-run replan is needed ──
            if (
                plan_result
                and scratchpad
                and escalation
                and agent_config.planning_enabled
                and not readonly_mode
            ):
                from robothor.engine.planner import should_replan as _should_replan

                budget_pct = 0.0
                if session.run.token_budget > 0:
                    used = session.run.input_tokens + session.run.output_tokens
                    budget_pct = used / session.run.token_budget

                if _should_replan(scratchpad, plan_result, escalation, _replan_count, budget_pct):
                    from robothor.engine.planner import format_plan_context, replan

                    new_plan = await replan(
                        plan_result,
                        scratchpad,
                        models[0],
                        fallback_models=models[1:],  # [1:2] missed the offline tier
                    )
                    if new_plan.success and new_plan.plan:
                        plan_result = new_plan
                        _replan_count += 1
                        scratchpad.set_plan(new_plan.plan)
                        # Non-fatal: replan formatting must not abort the run.
                        try:
                            plan_context = format_plan_context(new_plan)
                        except Exception as e:
                            plan_context = ""
                            logger.warning(
                                "Replan context formatting failed (non-fatal, "
                                "continuing without revised plan context): %s",
                                _sanitize(e),
                            )
                        # Dispatch REPLAN hook
                        if hook_registry:
                            with contextlib.suppress(Exception):
                                await hook_registry.dispatch(
                                    HookEvent.REPLAN,
                                    HookContext(
                                        event=HookEvent.REPLAN,
                                        agent_id=agent_config.id,
                                        run_id=session.run_id,
                                        metadata={"replan_count": _replan_count},
                                    ),
                                )
                        if plan_context:
                            session.messages.append(
                                {
                                    "role": ENGINE_CONTEXT_ROLE,
                                    "content": (
                                        f"[REVISED PLAN — attempt {_replan_count}]\n{plan_context}"
                                    ),
                                }
                            )

            # ── [CHECKPOINT] Save state ──
            if checkpoint and checkpoint.should_checkpoint():
                # Phase 5: pass the TodoList through so resume can rebuild it.
                # Without this the checklist was silently dropped on resume.
                todo_state: dict[str, Any] | None = None
                if session.todo_list:
                    try:
                        todo_state = session.todo_list.to_dict()
                    except Exception:
                        todo_state = None
                checkpoint.save(
                    step_number=session._step_counter,
                    messages=session.messages,
                    scratchpad=scratchpad.to_dict() if scratchpad else None,
                    plan=plan_result.raw if plan_result and hasattr(plan_result, "raw") else None,
                    todo_list=todo_state,
                )
                # Dispatch CHECKPOINT hook
                if hook_registry:
                    with contextlib.suppress(Exception):
                        await hook_registry.dispatch(
                            HookEvent.CHECKPOINT,
                            HookContext(
                                event=HookEvent.CHECKPOINT,
                                agent_id=agent_config.id,
                                run_id=session.run_id,
                                metadata={"step_number": session._step_counter},
                            ),
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
