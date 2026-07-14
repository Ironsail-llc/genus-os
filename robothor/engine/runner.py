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
import functools
import json
import logging
import os
import re
import time
import traceback
from typing import TYPE_CHECKING, Any

import litellm

from robothor.engine.config import (
    EngineConfig,
    _prompt_cache,
    build_system_prompt,
    load_agent_config,
)

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.llm_client import LLMClient  # noqa: E402
from robothor.engine.models import (
    AgentConfig,
    AgentRun,
    DeliveryMode,
    RunStep,
    SpawnContext,
    StepType,
    TriggerType,
)
from robothor.engine.prompts import (
    DEEP_PLAN_PREAMBLE,
    DEEP_PLAN_SUFFIX,
    EXECUTION_MODE_PREAMBLE,
    PLAN_MODE_PREAMBLE,
    PLAN_MODE_SUFFIX,
)

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.sanitize import sanitize_log as _sanitize  # noqa: E402
from robothor.engine.session import ENGINE_CONTEXT_ROLE, AgentSession
from robothor.engine.stall_watchdog import (
    _active_watchdog_var,
    _build_cancel_diagnostic,
    _fleet_wallclock_ceiling,
    _StallWatchdog,
)
from robothor.engine.tools import get_registry
from robothor.engine.tracking import create_run, create_step, create_steps_batch, update_run


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

# Fleet-wide runaway-token thresholds. Applied to the cumulative
# session.run.input_tokens + session.run.output_tokens across the run.
#   - Crossing ALERT fires a one-time Telegram warning so the operator can
#     decide whether to intervene.
#   - Reaching HARD_CAP stops the loop cleanly with budget_exhausted=True.
# These are fleet-wide constants (not per-agent configurable) so a
# misconfigured manifest can never disable the protection. A main run at
# Apr 22 16:07 consumed 3.2M input tokens before hitting the 86400s circuit
# breaker; this guard would have stopped it at 5M.
RUNAWAY_TOKEN_ALERT = 500_000
RUNAWAY_TOKEN_HARD_CAP = 5_000_000


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Trigger types that run with no interactive human and are therefore governed by
# the agent's service_role under the RBAC ladder (see the system-run gate in
# _run_loop). This is an ALLOWLIST on purpose: interactive surfaces (telegram,
# webchat, slack, ide, manual, webhook, channel_event) are gated by the dispatch
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
    }
)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True

# Register custom pricing so litellm.completion_cost() prices our models.
# Single-sourced from model_registry._MODEL_REGISTRY (G6) when Rip 17 is on;
# otherwise the legacy two-model block, preserved inside the function.
from robothor.engine.model_registry import register_pricing_with_litellm  # noqa: E402

register_pricing_with_litellm()


def _agent_holds_exec(config: AgentConfig) -> bool:
    """True if the agent can call the ``exec`` tool (i.e. touches the host shell).

    An empty ``tools_allowed`` means the agent receives the full tool set
    (including ``exec``); a ``tools_denied`` entry removes it.
    """
    denied = set(config.tools_denied or [])
    if "exec" in denied:
        return False
    allowed = config.tools_allowed or []
    return "exec" in allowed or not allowed


def _resolve_sandbox_decision(config: AgentConfig, mode: str) -> str:
    """Decide sandboxing for a run. Returns 'docker' | 'observe' | 'host'.

    'docker' = start a Docker sandbox; 'observe' = an exec-holding agent that
    WOULD be sandboxed but runs on host (caller logs it); 'host' = run on host.
    ``mode`` is ``sandbox_default_mode()``. Explicit manifest ``sandbox: docker``
    always sandboxes; ``sandbox: host`` always opts out.
    """
    if config.sandbox == "docker":
        return "docker"
    if config.sandbox == "host":
        return "host"
    if mode != "off" and _agent_holds_exec(config):
        return "docker" if mode == "enforce" else "observe"
    return "host"


def _escalate_unfinished_todos(
    todos: Any,
    parent_task_id: str | None,
    agent_id: str,
    tenant_id: str = "",
    agent_config: Any = None,
    run_id: str | None = None,
) -> bool:
    """Lift unfinished todo_write items back to the CRM parent task so the
    next heartbeat's planner picks up where this run left off.

    Closes the Stage-5 "full circle" gap: a run that exhausted iterations
    with items still pending or in_progress must propagate that work to
    the thread pool — silently dropping it is the prior failure mode.

    Behavior:
      - None/empty todo list → no-op, returns False.
      - No parent_task_id → no-op (list was standalone).
      - All items completed → no-op (run finished cleanly).
      - Otherwise: write set_next_action("Continue: <first_unfinished>")
        on the parent. If the parent isn't already tagged `thread`, add
        the tag and seed `objective` from the title if empty. Non-
        destructive — never touches status, owner, or other tags.

    Phase 3 addition: when ``agent_config.todo_list_enabled`` and
    ``agent_config.task_protocol`` are both true AND the
    ``ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED`` env is "1", unfinished
    items are ALSO promoted to real CRM subtasks (idempotent via content
    hash). The ``next_action`` write above stays — promotion is additive.
    """
    if todos is None:
        return False
    items = getattr(todos, "items", None) or []
    if not items:
        return False
    if not parent_task_id:
        return False

    unfinished = [it for it in items if getattr(it, "status", "") in ("pending", "in_progress")]
    if not unfinished:
        return False

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal

    effective_tenant = tenant_id or DEFAULT_TENANT

    try:
        parent = dal.get_task(parent_task_id, tenant_id=effective_tenant)
    except Exception as e:
        logger.warning("todo escalation: get_task failed for %s: %s", _sanitize(parent_task_id), e)
        return False
    if not parent:
        return False

    first = unfinished[0]
    next_action = f"Continue: {getattr(first, 'content', '').strip()}"[:500]

    dal.set_next_action(
        task_id=parent_task_id,
        next_action=next_action,
        agent=agent_id,
        by="runtime",
        tenant_id=effective_tenant,
    )

    existing_tags = parent.get("tags") or []
    if "thread" not in existing_tags:
        new_tags = list(existing_tags) + ["thread"]
        update_kwargs: dict[str, Any] = {
            "task_id": parent_task_id,
            "tags": new_tags,
            "changed_by": agent_id,
            "tenant_id": effective_tenant,
        }
        # Seed objective from title if empty — the planner needs something
        # to plan against on the next beat.
        if not (parent.get("objective") or "").strip():
            update_kwargs["objective"] = parent.get("title") or ""
        dal.update_task(**update_kwargs)
        # Refresh the local parent dict so the promotion step below sees the
        # freshly added `thread` tag.
        parent["tags"] = new_tags

    # Phase 3 — promote unfinished items to real subtasks (best-effort).
    if agent_config is not None:
        try:
            from robothor.engine.todo_promotion import promote_unfinished_items

            promote_unfinished_items(
                parent=parent,
                items=unfinished,
                agent_config=agent_config,
                agent_id=agent_id,
                run_id=run_id or "",
                tenant_id=effective_tenant,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("todo promotion error (non-fatal): %s", _sanitize(e))

    return True


class AgentRunner:
    """Executes agents: builds prompt, enters tool loop, tracks everything."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.registry = get_registry()
        # LLM dispatch/fallback/cost/streaming + message hygiene. Extracted
        # from this class (Phase A / Slice 1); stateless across runs.
        self._llm = LLMClient()

    @property
    def _active_watchdog(self) -> _StallWatchdog | None:
        """The current task's stall watchdog (per-run, not per-singleton).

        Read-only view over ``_active_watchdog_var`` so the existing touch sites
        keep working unchanged; ``execute`` sets/resets the ContextVar directly.
        """
        return _active_watchdog_var.get()

    # ── Upgrade-plan hook points (Phase 0 foundation) ──────────────────
    # These two methods are no-ops by default. Future rips wire their
    # behavior here without further surgery on _run_loop or _finish_run:
    #
    #   Rip 1  (background-review fork)  → schedules a forked agent
    #          in _after_response_delivered when memory/skill nudge
    #          counters trip.
    #   Rip 9  (interrupt/steer)         → drains pending steer in
    #          _after_iteration so the next API call sees it.
    #   Rip 10 (trajectory capture)      → persists session messages
    #          in _after_response_delivered when sampling fires.
    #
    # Hook methods are kept on AgentRunner (rather than a registry) so
    # subclasses can override directly and so the hot-path call sites
    # stay one line each.

    async def _after_iteration(
        self,
        session: AgentSession,
        iteration: int,
        prev_tool_names: list[str] | None = None,
    ) -> None:
        """Per-iteration hook. Called at the end of every tool loop turn.

        Default: no-op. Future rips override to advance session
        counters, drain steers, or update watchdog state. Must stay
        non-blocking and exception-safe — the caller suppresses
        exceptions to keep the loop alive.
        """
        # G3 (Rip 9 wiring): drain any operator steer queued via
        # interrupt_api.steer_session into the conversation so the next LLM
        # call sees it. Injected as a *user* turn — never the system prompt —
        # so the cached prefix stays intact (prompt-cache discipline).
        steer = session.consume_pending_steer()
        if steer:
            session.messages.append({"role": "user", "content": f"[steer] {steer}"})
            # A steer is a user turn too — count it for the memory-review nudge.
            session._turns_since_memory += 1

    def _after_response_delivered(
        self,
        session: AgentSession,
        run: AgentRun,
    ) -> None:
        """Post-response hook. Called from _finish_run before return.

        Default behaviour (Rip 1): when ``ROBOTHOR_RIP_1_ENABLED=1``
        and the session's nudge counters have tripped, schedule the
        background-review fork as a non-blocking asyncio task. The
        fork runs concurrently while ``_finish_run`` finishes
        persisting the foreground response.

        The hook is sync because ``_finish_run`` is sync. The actual
        fork uses ``asyncio.create_task`` inside
        ``background_review.fire_and_forget``; when no loop is running
        (e.g. tests calling _finish_run directly without an event
        loop), the call falls silent rather than raising.

        Subclasses that need additional behaviour can override this
        and call ``super()._after_response_delivered(...)`` to keep
        the Rip 1 wiring.
        """
        from robothor.engine import background_review

        background_review.fire_and_forget(session)

        # Rip 10: persist trajectory transcript (sampling controlled
        # by ROBOTHOR_TRAJECTORY_SAMPLE; 0.0 default → never).
        try:
            from robothor.engine.trajectory import save_trajectory_for_run

            save_trajectory_for_run(session, run)
        except Exception as exc:  # noqa: BLE001 — never block run finalization
            logger.debug("trajectory: post-response save raised: %s", exc)

        # PR-3a: evidence-based completion contracts. Flag-gated
        # off→observe→enforce (default off). When the run's final output
        # claims a session goal is done, verify the claim against recorded,
        # validated evidence rather than trusting the model's say-so.
        from robothor.engine.feature_flags import completion_contract_mode

        cc_mode = completion_contract_mode()
        if cc_mode != "off":
            try:
                from robothor.engine.completion_contract import check_completion_contract

                verdict = check_completion_contract(run, self.config)
            except Exception as exc:  # noqa: BLE001 — never block run finalization
                logger.debug("completion contract check raised: %s", exc)
                verdict = None
            if verdict is not None and verdict.status == "missing":
                reason = "; ".join(verdict.missing)
                try:
                    from robothor.engine.tracking import log_guardrail_event

                    log_guardrail_event(
                        run_id=run.id,
                        guardrail_name="completion_contract",
                        action="blocked" if cc_mode == "enforce" else "observed",
                        reason=reason,
                        mode=cc_mode,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("completion contract event log failed: %s", exc)
                if cc_mode == "alert":
                    # Middle rung: the claim stands, but the operator hears about it.
                    from robothor.engine.feature_flags import notify_guardrail_alert

                    notify_guardrail_alert(
                        guardrail_name="completion_contract",
                        agent_id=run.agent_id,
                        reason=reason,
                        tenant_id=getattr(run, "tenant_id", "") or "",
                    )
                if cc_mode == "enforce":
                    try:
                        from robothor.crm import dal

                        dal.set_next_action(
                            task_id=verdict.goal_id,
                            next_action=f"Provide evidence: {reason}"[:500],
                            agent=run.agent_id,
                            by="completion_contract",
                            tenant_id=run.tenant_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "completion contract enforce set_next_action failed: %s", exc
                        )

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
    ) -> AgentRun:
        """Execute an agent with the given message.

        Args:
            execution_mode: When True, prepend EXECUTION_MODE_PREAMBLE to
                system prompt to enforce plan execution (no re-planning).
        Returns the completed AgentRun with full metadata.
        """
        resolved_tenant = tenant_id or self.config.tenant_id

        # Load agent config from manifest if not provided
        if agent_config is None:
            agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if agent_config is None:
            logger.error("Agent config not found: %s", _sanitize(agent_id))
            session = AgentSession(agent_id, trigger_type, trigger_detail, resolved_tenant)
            session.start("", message, [])
            return session.fail(f"Agent config not found: {agent_id}")

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
        session.run.user_id = user_id
        session.run.user_role = user_role

        # Benchmark sandbox marker — when the parent (typically benchmark-runner
        # via _benchmark_run) stamps the child_config with is_benchmark=True,
        # propagate onto the AgentRun so side-effect tool wrappers (gws CLI
        # bypass, etc.) can short-circuit. Belt to the L1 allow-list
        # suspenders in robothor/engine/tools/handlers/benchmark.py.
        session.run.is_benchmark = bool(getattr(agent_config, "is_benchmark", False))

        # Sub-agent: link to parent run + inherit user identity
        if spawn_context:
            session.run.parent_run_id = spawn_context.parent_run_id
            session.run.nesting_depth = spawn_context.nesting_depth + 1
            if not user_id and spawn_context.user_id:
                session.run.user_id = spawn_context.user_id
                session.run.user_role = spawn_context.user_role
            # Contact 360 linkage — inherit parent's person.
            if spawn_context.person_id:
                session.run.person_id = spawn_context.person_id

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
                logger.debug("person_id resolution failed for %s: %s", agent_id, e)

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
        stall_timeout = getattr(agent_config, "stall_timeout_seconds", 300)
        # Absolute wall-clock ceiling. When an agent sets timeout_seconds=0
        # ("no cap" — e.g. main's heartbeat/worker) it previously had NO hard
        # bound, so a slow-but-not-stalled run could grind for 25–128 min
        # (audit 2026-05-29). Fall back to a generous fleet ceiling instead of
        # leaving it unbounded; one turn never legitimately needs this long.
        effective_hard_timeout = agent_config.timeout_seconds
        if effective_hard_timeout <= 0:
            effective_hard_timeout = _fleet_wallclock_ceiling()
        hard_timeout = effective_hard_timeout if effective_hard_timeout > 0 else None
        early_stall_timeout = getattr(agent_config, "early_stall_timeout_seconds", 0)
        watchdog = _StallWatchdog(
            stall_timeout=stall_timeout,
            hard_timeout=effective_hard_timeout,
            early_stall_timeout=early_stall_timeout,
        )
        # Bind the watchdog to THIS task's context (see _active_watchdog_var).
        # Saved token is reset in the run-loop finally so a nested run restores
        # the parent's watchdog instead of clobbering it.
        _wd_token = _active_watchdog_var.set(watchdog)

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
            # Only warmup on first message of a session — follow-ups already
            # have memory blocks and entity context in conversation history.
            if not conversation_history:
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
                    await asyncio.get_running_loop().run_in_executor(None, create_run, _blocked_run)
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
                return self._finish_run(
                    _blocked_run,
                    trace=None,
                    agent_config=agent_config,
                    session=session,
                    spawn_context=spawn_context,
                )
            if _inj_finding:
                with contextlib.suppress(Exception):
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

        # ── Load business adapters (external MCP servers) ──
        try:
            from robothor.engine.adapters import get_adapters_for_agent
            from robothor.engine.mcp_client import configure_mcp_servers, register_adapter

            # Wire up v2.mcp_servers from manifest (previously dead code)
            if agent_config.mcp_servers:
                configure_mcp_servers(agent_config.mcp_servers)

            # Load and register business adapters
            adapters = get_adapters_for_agent(agent_id)
            for adapter in adapters:
                register_adapter(adapter)
            if adapters:
                await self.registry.register_adapter_tools(adapters)
        except Exception as e:
            logger.warning("Adapter loading failed (non-fatal): %s", _sanitize(e))
        watchdog.touch("adapters_loaded")

        # Get filtered tools for this agent
        if readonly_mode:
            # Plan mode: sandwich pattern — prepend constraints BEFORE identity,
            # append reminder AFTER, so plan rules aren't buried by SOUL.md directives.
            tool_schemas = self.registry.build_readonly_for_agent(agent_config)
            tool_names = self.registry.get_readonly_tool_names(agent_config)
            if deep_plan:
                system_prompt = DEEP_PLAN_PREAMBLE + system_prompt + DEEP_PLAN_SUFFIX
            else:
                # Inject actual tool names into the preamble
                tool_list_str = (
                    ", ".join(f"`{t}`" for t in sorted(tool_names)) if tool_names else "(none)"
                )
                preamble = PLAN_MODE_PREAMBLE.replace("{tool_names_placeholder}", tool_list_str)
                system_prompt = preamble + system_prompt + PLAN_MODE_SUFFIX
        else:
            tool_schemas = self.registry.build_for_agent(agent_config)
            tool_names = self.registry.get_tool_names(agent_config)

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
                try:
                    await asyncio.get_running_loop().run_in_executor(None, create_run, session.run)
                except Exception as e:
                    logger.warning("Failed to record run start: %s", _sanitize(e))

                # Auto-create CRM task if configured (skip for sub-agent runs)
                if agent_config.auto_task and not spawn_context:
                    try:
                        from robothor.crm.dal import create_task as dal_create_task

                        task_id = await asyncio.get_running_loop().run_in_executor(
                            None,
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
                        from robothor.engine.planner import format_plan_context

                        plan_context = format_plan_context(plan_result)
                        if plan_context:
                            session.messages.append(
                                {"role": ENGINE_CONTEXT_ROLE, "content": plan_context}
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

                _sb_decision = _resolve_sandbox_decision(agent_config, sandbox_default_mode())
                if _sb_decision == "observe":
                    with contextlib.suppress(Exception):
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
                sandbox = None
                if _sb_decision == "docker":
                    from robothor.engine.sandbox import Sandbox, SandboxMode, set_current_sandbox

                    sandbox = Sandbox(mode=SandboxMode.DOCKER, run_id=session.run.id)
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
                        # not have. (On this box the engine user is not in the
                        # docker group, so start() cannot succeed at all — the
                        # old behavior turned "enforce" into pure theater.)
                        # Under observe, degrading to the host IS the contract.
                        if sandbox_default_mode() == "enforce":
                            _sb_reason = f"sandbox required but could not be started: {e}"
                            try:
                                from robothor.engine.tracking import log_guardrail_event

                                log_guardrail_event(
                                    run_id=session.run.id,
                                    guardrail_name="sandbox_default",
                                    action="blocked",
                                    tool_name="exec",
                                    reason=_sb_reason,
                                    mode="enforce",
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
                    # Cleanup sandbox if created
                    if sandbox:
                        try:
                            await sandbox.stop()
                        except Exception as e:
                            logger.warning("Sandbox cleanup failed: %s", _sanitize(e))
                        from robothor.engine.sandbox import set_current_sandbox

                        set_current_sandbox(None)

        except (TimeoutError, asyncio.CancelledError):
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
            ht = agent_config.timeout_seconds
            reason = abort_reason or (
                f"Circuit-breaker hard timeout ({ht}s); last activity: {watchdog.last_activity_desc}"
                if ht > 0
                else f"Run cancelled externally; last activity: {watchdog.last_activity_desc}"
            )
            logger.warning("Agent %s cancelled: %s", _sanitize(agent_id), _sanitize(reason))
            session.record_error(reason)
            # Diagnostic dump for the noon-storm investigation. Captures
            # who else is alive at cancel time, the watchdog's last touch,
            # and elapsed-since-start. Lands in agent_runs.error_traceback.
            diag = _build_cancel_diagnostic(watchdog, agent_id)
            return self._finish_run(
                session.timeout(reason=reason, traceback=diag),
                trace=trace,
                agent_config=agent_config,
                session=session,
                spawn_context=spawn_context,
            )
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

        Returns:
            AgentRun with output_text set to the RLM response, cost unified.
        """
        import uuid

        from robothor.engine.session import AgentSession

        agent_id = "main"
        session = AgentSession(
            agent_id=agent_id,
            trigger_type=TriggerType.MANUAL,
            trigger_detail="deep_reason",
            tenant_id=self.config.tenant_id,
        )
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
                parent_run_id=session.run.id,
                parent_agent_id=agent_config.id,
                correlation_id=session.run.correlation_id or str(uuid.uuid4()),
                nesting_depth=0,
                max_nesting_depth=agent_config.max_nesting_depth,
                max_spawn_batch=agent_config.max_spawn_batch,
                remaining_token_budget=session.run.token_budget,
                parent_trace_id=trace.trace_id if trace else "",
                parent_span_id="",
                person_id=session.run.person_id,
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
            HookAction,
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
        _runaway_alerted = False  # one-shot latch for 500K alert

        while True:
            # ── [STEER / INTERRUPT] live operator influence (Rip 9) ──
            # An external caller may have set a steer (inject + continue) or an
            # interrupt (halt) on this session via session_registry.
            _steer_text = session.consume_pending_steer()
            if _steer_text:
                session.messages.append(
                    {"role": "user", "content": f"[operator steering update]\n{_steer_text}"}
                )
                logger.info("Live steer injected into run %s", session.run_id)
            # ── [INTERRUPT] Operator halt requested via interrupt_api (Rip 9 / G3) ──
            # Consume any pending interrupt and stop the run gracefully: record a
            # distinct terminal state (CANCELLED, not COMPLETED/FAILED, verifier
            # skipped) AND an outcome note so the halt is visible. The message may
            # be "" when the operator halted without text, or None if no interrupt.
            _interrupt_msg = session.consume_interrupt()
            if _interrupt_msg is not None:
                note = (
                    f"Run interrupted by operator: {_interrupt_msg}"
                    if _interrupt_msg
                    else "Run interrupted by operator"
                )
                session.messages.append({"role": "user", "content": f"[operator interrupt] {note}"})
                session.run.outcome_notes = (
                    f"{session.run.outcome_notes}; {note}" if session.run.outcome_notes else note
                )
                session.mark_interrupted(note)
                logger.info("Run %s interrupted by operator", session.run_id)
                return

            # ── [WATCHDOG] Cooperative abort — catches stalls even when task.cancel() fails ──
            if self._active_watchdog and self._active_watchdog.should_abort:
                logger.warning(
                    "Run loop aborting: watchdog flagged abort — %s",
                    self._active_watchdog.abort_reason,
                )
                session.record_error(self._active_watchdog.abort_reason)
                return

            # ── [RUNAWAY] Fleet-wide token guard (500K alert, 5M hard cap) ──
            _used_tokens = (session.run.input_tokens or 0) + (session.run.output_tokens or 0)
            if _used_tokens >= RUNAWAY_TOKEN_HARD_CAP:
                reason = f"runaway_token_cap_hit ({_used_tokens}/{RUNAWAY_TOKEN_HARD_CAP})"
                logger.error(
                    "Runaway-token hard cap hit: agent=%s run=%s tokens=%d",
                    agent_config.id,
                    session.run_id,
                    _used_tokens,
                )
                # Fire-and-forget alert — don't block the stop path. Use the task
                # registry's spawn (not bare create_task, which the loop only
                # weakly references and can GC before it runs — losing exactly
                # the alert we can least afford to lose; audit 2026-05-29).
                try:
                    from robothor.engine.alerts import alert as _alert
                    from robothor.engine.task_registry import get_task_registry

                    get_task_registry().spawn(
                        _alert(
                            "critical",
                            f"Runaway-token hard cap: {agent_config.id}",
                            f"run_id={session.run_id} tokens={_used_tokens:,} "
                            f"model={session.run.model_used}",
                        ),
                        name=f"runaway-hardcap-alert:{agent_config.id}",
                    )
                except Exception:
                    logger.debug("Runaway-token alert dispatch failed", exc_info=True)
                session.run.budget_exhausted = True
                session.record_error(reason)
                return
            if not _runaway_alerted and _used_tokens >= RUNAWAY_TOKEN_ALERT:
                _runaway_alerted = True
                logger.warning(
                    "Runaway-token alert: agent=%s run=%s tokens=%d",
                    agent_config.id,
                    session.run_id,
                    _used_tokens,
                )
                try:
                    from robothor.engine.alerts import alert as _alert
                    from robothor.engine.task_registry import get_task_registry

                    get_task_registry().spawn(
                        _alert(
                            "warning",
                            f"Runaway-token alert: {agent_config.id}",
                            f"run_id={session.run_id} tokens={_used_tokens:,} "
                            f"(hard cap at {RUNAWAY_TOKEN_HARD_CAP:,}) "
                            f"model={session.run.model_used}",
                        ),
                        name=f"runaway-alert:{agent_config.id}",
                    )
                except Exception:
                    logger.debug("Runaway-token alert dispatch failed", exc_info=True)

            # ── [SAFETY VALVE] Absolute iteration cap (infinite-loop protection) ──
            # safety_cap=0 is the manifest sentinel for "no cap" (main.yaml sets
            # this for heartbeat + worker per operator directive 2026-04-20). The
            # check only fires when the cap is positive.
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

            # ── [EAGER COMPRESSION] Thin previous iterations' tool results ──
            if agent_config.eager_tool_compression and _iteration > 0:
                chars_saved = session.thin_previous_tool_results(
                    protect_after_index=_pre_iteration_msg_idx,
                )
                if chars_saved > 0:
                    logger.debug(
                        "Eager tool compression saved ~%d tokens",
                        chars_saved // 4,
                    )

            # ── [PROACTIVE COMPACTION] Compress before hitting the 75% cliff ──
            if _iteration > 0 and _iteration % 5 == 0:
                try:
                    from robothor.engine.context import estimate_tokens, maybe_compress
                    from robothor.engine.model_registry import get_model_limits

                    est_tokens = estimate_tokens(session.messages)
                    # G2b: size against the model that will actually be tried
                    # next (first non-broken), not the configured primary —
                    # otherwise a run on a smaller-window fallback compacts at
                    # the primary's (larger) threshold and can overflow.
                    model_limits = get_model_limits(LLMClient.sizing_model(models, broken_models))
                    proactive_threshold = int(model_limits.max_input_tokens * 0.50)
                    if est_tokens > proactive_threshold:
                        pre_len = len(session.messages)

                        # Dispatch PRE_COMPACTION hook
                        if hook_registry:
                            with contextlib.suppress(Exception):
                                await hook_registry.dispatch(
                                    HookEvent.PRE_COMPACTION,
                                    HookContext(
                                        event=HookEvent.PRE_COMPACTION,
                                        agent_id=agent_config.id,
                                        run_id=session.run_id,
                                        metadata={
                                            "est_tokens": est_tokens,
                                            "threshold": proactive_threshold,
                                            "message_count": pre_len,
                                        },
                                    ),
                                )

                        session.messages[:] = await maybe_compress(
                            session.messages,
                            models,
                            threshold=proactive_threshold,
                        )
                        logger.info(
                            "Proactive compaction at iter %d: %d→%d messages "
                            "(est %d tokens, threshold %d)",
                            _iteration,
                            pre_len,
                            len(session.messages),
                            est_tokens,
                            proactive_threshold,
                        )

                        # Dispatch POST_COMPACTION hook
                        if hook_registry:
                            with contextlib.suppress(Exception):
                                await hook_registry.dispatch(
                                    HookEvent.POST_COMPACTION,
                                    HookContext(
                                        event=HookEvent.POST_COMPACTION,
                                        agent_id=agent_config.id,
                                        run_id=session.run_id,
                                        metadata={
                                            "pre_message_count": pre_len,
                                            "post_message_count": len(session.messages),
                                        },
                                    ),
                                )
                except Exception as e:
                    logger.warning("Proactive compaction failed: %s", _sanitize(e))

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

                # ── [PLAN MODE GUARD] Runtime enforcement ──
                # Belt-and-suspenders: even though schemas are filtered,
                # block any non-readonly tool call during plan mode at runtime.
                if readonly_mode and tool_name not in _readonly_tool_set:
                    guard_msg = (
                        f"Tool '{tool_name}' is not available in plan mode. "
                        "Only read-only tools can be used during planning."
                    )
                    session.record_tool_call(
                        tool_name=tool_name,
                        tool_input=tool_args,
                        tool_output={"error": guard_msg, "guard": "plan_mode"},
                        tool_call_id=tc.id,
                        error_message=guard_msg,
                    )
                    iteration_errors.append((tool_name, guard_msg, None))
                    if scratchpad:
                        scratchpad.record_tool_call(tool_name, error=guard_msg)
                    continue

                # ── [TOOLS_ALLOWED GUARD] Runtime enforcement ──
                # Belt-and-suspenders: even though schemas are filtered,
                # block any tool call not in the agent's allowed set.
                if _allowed_tool_set and tool_name not in _allowed_tool_set:
                    guard_msg = f"Tool '{tool_name}' is not available to this agent."
                    session.record_tool_call(
                        tool_name=tool_name,
                        tool_input=tool_args,
                        tool_output={"error": guard_msg, "guard": "tools_allowed"},
                        tool_call_id=tc.id,
                        error_message=guard_msg,
                    )
                    iteration_errors.append((tool_name, guard_msg, None))
                    if scratchpad:
                        scratchpad.record_tool_call(tool_name, error=guard_msg)
                    continue

                # ── [HOOKS] Pre-tool-use lifecycle hook ──
                if hook_registry:
                    try:
                        pre_tool_ctx = HookContext(
                            event=HookEvent.PRE_TOOL_USE,
                            agent_id=agent_config.id,
                            run_id=session.run.id,
                            tool_name=tool_name,
                            tool_args=tool_args,
                        )
                        pre_hr = await hook_registry.dispatch(HookEvent.PRE_TOOL_USE, pre_tool_ctx)
                        if pre_hr.action == HookAction.BLOCK:
                            block_msg = f"Blocked by lifecycle hook: {pre_hr.reason}"
                            session.record_tool_call(
                                tool_name=tool_name,
                                tool_input=tool_args,
                                tool_output={"error": block_msg, "hook": "pre_tool_use"},
                                tool_call_id=tc.id,
                                error_message=block_msg,
                            )
                            iteration_errors.append((tool_name, block_msg, None))
                            if scratchpad:
                                scratchpad.record_tool_call(tool_name, error=block_msg)
                            if escalation:
                                escalation.record_error()
                            continue
                        if pre_hr.action == HookAction.MODIFY and pre_hr.modified_args:
                            tool_args = pre_hr.modified_args
                    except Exception as e:
                        logger.warning(
                            "PRE_TOOL_USE hook error for %s: %s", _sanitize(tool_name), _sanitize(e)
                        )

                # ── [GUARDRAILS] Pre-execution check ──
                if guardrail_engine:
                    gr = guardrail_engine.check_pre_execution(
                        tool_name,
                        tool_args,
                        agent_id=agent_config.id,
                        prior_steps=session.run.steps,
                    )
                    # ── [OBSERVE] Allowed, but a rollout-gated guardrail would
                    # have blocked this in enforce mode. Persist it: a soak that
                    # records nothing cannot distinguish "clean" from "blind".
                    if gr.allowed and gr.action == "observed":
                        with contextlib.suppress(Exception):
                            from robothor.engine.tracking import log_guardrail_event

                            log_guardrail_event(
                                run_id=session.run.id,
                                guardrail_name=gr.guardrail_name,
                                action="observed",
                                tool_name=tool_name,
                                reason=gr.reason,
                                mode="observe",
                                step_number=len(session.run.steps),
                            )
                    if not gr.allowed:
                        # ── [HUMAN APPROVAL] Escalation for opt-in agents ──
                        if gr.action == "escalate":
                            from robothor.engine.permission_escalation import (
                                get_permission_manager,
                            )

                            mgr = get_permission_manager()
                            if mgr:
                                approved = await mgr.request_approval(
                                    agent_id=agent_config.id,
                                    run_id=session.run_id,
                                    tool_name=tool_name,
                                    tool_args=tool_args,
                                    guardrail_name=gr.guardrail_name,
                                    reason=gr.reason,
                                    timeout_seconds=agent_config.human_approval_timeout,
                                )
                                if approved:
                                    pass  # fall through to execute tool
                                else:
                                    gr_error_msg = (
                                        f"Denied by operator ({gr.guardrail_name}): {gr.reason}"
                                    )
                                    session.record_tool_call(
                                        tool_name=tool_name,
                                        tool_input=tool_args,
                                        tool_output={"error": gr_error_msg},
                                        tool_call_id=tc.id,
                                        error_message=gr_error_msg,
                                    )
                                    if scratchpad:
                                        scratchpad.record_tool_call(tool_name, error=gr_error_msg)
                                    continue
                            elif agent_config.human_approval_fail_open:
                                pass  # opted-in unattended autonomy: auto-approve
                            else:
                                # No approver reachable. Legacy behavior auto-
                                # approves; ROBOTHOR_APPROVAL_* makes this fail
                                # closed (observe logs the would-deny; enforce
                                # denies the tool).
                                from robothor.engine.feature_flags import approval_mode
                                from robothor.engine.permission_escalation import (
                                    fail_closed_on_missing_manager,
                                )

                                _appr_mode = approval_mode()
                                if _appr_mode != "off":
                                    with contextlib.suppress(Exception):
                                        from robothor.engine.tracking import log_guardrail_event

                                        log_guardrail_event(
                                            run_id=session.run.id,
                                            guardrail_name=gr.guardrail_name,
                                            action="blocked"
                                            if _appr_mode == "enforce"
                                            else "observed",
                                            tool_name=tool_name,
                                            reason="human approval required but no approver reachable",
                                            mode=_appr_mode,
                                            step_number=len(session.run.steps),
                                        )
                                if fail_closed_on_missing_manager():
                                    gr_error_msg = (
                                        f"Denied — human approval required for "
                                        f"{gr.guardrail_name} but no approver is reachable"
                                    )
                                    session.record_tool_call(
                                        tool_name=tool_name,
                                        tool_input=tool_args,
                                        tool_output={"error": gr_error_msg},
                                        tool_call_id=tc.id,
                                        error_message=gr_error_msg,
                                    )
                                    if scratchpad:
                                        scratchpad.record_tool_call(tool_name, error=gr_error_msg)
                                    if escalation:
                                        escalation.record_error()
                                    continue
                                # otherwise auto-approve (legacy) and fall through
                        else:
                            gr_error_msg = (
                                f"Blocked by guardrail ({gr.guardrail_name}): {gr.reason}"
                            )
                            session.record_tool_call(
                                tool_name=tool_name,
                                tool_input=tool_args,
                                tool_output={"error": gr_error_msg, "guardrail": gr.guardrail_name},
                                tool_call_id=tc.id,
                                error_message=gr_error_msg,
                            )
                            iteration_errors.append((tool_name, gr_error_msg, None))
                            with contextlib.suppress(Exception):
                                from robothor.engine.tracking import (
                                    log_guardrail_event,
                                    log_tool_event,
                                )

                                log_tool_event(
                                    run_id=session.run.id,
                                    tool_name=tool_name,
                                    duration_ms=0,
                                    success=False,
                                    error_type="guardrail_blocked",
                                )
                                # Make the guardrail block visible in the audit
                                # table the health dashboard reads (PR-1).
                                log_guardrail_event(
                                    run_id=session.run.id,
                                    guardrail_name=gr.guardrail_name,
                                    action="blocked",
                                    tool_name=tool_name,
                                    reason=gr.reason,
                                    mode="enforce",
                                    step_number=len(session.run.steps),
                                )
                            if scratchpad:
                                scratchpad.record_tool_call(tool_name, error=gr_error_msg)
                            if escalation:
                                escalation.record_error()
                            continue

                # ── [RBAC] System-run permission gate ──
                # Only genuinely autonomous, no-interactive-user runs are governed
                # by the (permissive) service_role here. Interactive surfaces
                # (telegram/webchat/slack/ide/manual/webhook/channel) are gated by
                # the dispatch user_role check instead — an ALLOWLIST so a new
                # trigger type defaults to the restrictive user path, not
                # service_role. See _SYSTEM_TRIGGER_TYPES.
                if agent_config is not None and session.run.trigger_type in _SYSTEM_TRIGGER_TYPES:
                    from robothor.engine.feature_flags import rbac_enforcement_mode
                    from robothor.engine.permissions import classify_system_tool_access

                    _rbac_mode = rbac_enforcement_mode()
                    # check_tool_permission opens a sync DB connection; keep it off
                    # the event loop so a slow round-trip can't stall the engine.
                    _rbac_action, _rbac_reason = await asyncio.to_thread(
                        classify_system_tool_access,
                        agent_config.service_role,
                        session.run.tenant_id,
                        tool_name,
                        _rbac_mode,
                    )
                    if _rbac_action != "allow":
                        with contextlib.suppress(Exception):
                            from robothor.engine.tracking import log_guardrail_event

                            log_guardrail_event(
                                run_id=session.run.id,
                                guardrail_name="rbac",
                                action="blocked" if _rbac_action == "block" else "observed",
                                tool_name=tool_name,
                                reason=_rbac_reason,
                                mode=_rbac_mode,
                                step_number=len(session.run.steps),
                            )
                        if _rbac_action == "block":
                            rbac_msg = f"Blocked by RBAC: {_rbac_reason}"
                            session.record_tool_call(
                                tool_name=tool_name,
                                tool_input=tool_args,
                                tool_output={"error": rbac_msg, "guardrail": "rbac"},
                                tool_call_id=tc.id,
                                error_message=rbac_msg,
                            )
                            iteration_errors.append((tool_name, rbac_msg, None))
                            if scratchpad:
                                scratchpad.record_tool_call(tool_name, error=rbac_msg)
                            if escalation:
                                escalation.record_error()
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
                _tool_timeout = getattr(agent_config, "tool_timeout_seconds", 120)
                # Benchmark and experiment tools legitimately run 5+ sub-agents;
                # raise their timeout floor to 600s regardless of agent-level setting.
                _LONG_RUNNING_TOOLS = frozenset(  # noqa: N806 — module-internal constant kept here for locality
                    {
                        "benchmark_run",
                        "experiment_measure",
                        "benchmark_compare",
                        "spawn_agent",
                        "spawn_agents",
                    }
                )
                if tool_name in _LONG_RUNNING_TOOLS:
                    _tool_timeout = max(_tool_timeout, 600)
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
                    )
                tool_elapsed = int((time.monotonic() - tool_start) * 1000)

                error_msg: str | None = result.get("error") if isinstance(result, dict) else None

                # ── [GUARDRAILS] Post-execution check ──
                if guardrail_engine and not error_msg:
                    post_gr = guardrail_engine.check_post_execution(tool_name, result)
                    if post_gr.action == "warned":
                        logger.warning("Guardrail warning for %s: %s", tool_name, post_gr.reason)

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

                # ── [ERROR CLASSIFICATION] Classify error type ──
                error_type = None
                if error_msg:
                    from robothor.engine.error_recovery import classify_error

                    error_type = classify_error(tool_name, error_msg)

                # ── [TOOL EVENTS] Log tool invocation for observability ──
                with contextlib.suppress(Exception):
                    from robothor.engine.tracking import log_tool_event

                    log_tool_event(
                        run_id=session.run.id,
                        tool_name=tool_name,
                        duration_ms=tool_elapsed,
                        success=error_msg is None,
                        error_type=error_type.value
                        if error_type and hasattr(error_type, "value")
                        else (str(error_type) if error_type else None),
                    )

                # ── [SCRATCHPAD] Record tool call ──
                if scratchpad:
                    scratchpad.record_tool_call(tool_name, error=error_msg)

                # ── [CIRCUIT BREAKER] Stop calling tools that keep failing ──
                if error_msg:
                    _tool_failures[tool_name] = _tool_failures.get(tool_name, 0) + 1
                    if _tool_failures[tool_name] >= 3:
                        session.messages.append(
                            {
                                "role": ENGINE_CONTEXT_ROLE,
                                "content": (
                                    f"[SYSTEM] Tool '{tool_name}' has failed "
                                    f"{_tool_failures[tool_name]} times this run. "
                                    "Do NOT call it again. Find an alternative "
                                    "approach or skip this step and move on."
                                ),
                            }
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
                        fallback_models=models[1:2],
                    )
                    if new_plan.success and new_plan.plan:
                        plan_result = new_plan
                        _replan_count += 1
                        scratchpad.set_plan(new_plan.plan)
                        plan_context = format_plan_context(new_plan)
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
                        session.messages.append(
                            {
                                "role": ENGINE_CONTEXT_ROLE,
                                "content": f"[REVISED PLAN — attempt {_replan_count}]\n{plan_context}",
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
                await asyncio.get_running_loop().run_in_executor(None, session.flush_new_steps_sync)
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

    async def _send_progress_report(
        self,
        session: Any,
        agent_config: Any,
        iteration: int,
    ) -> None:
        """Send a brief Telegram progress report during continuous execution."""
        try:
            from robothor.engine.delivery import get_telegram_sender

            sender = get_telegram_sender()
            if not sender:
                return

            # Summarize recent activity
            tool_calls = sum(
                1
                for m in session.messages[-50:]
                if m.get("role") == "assistant" and m.get("tool_calls")
            )
            cost = f"${session.run.total_cost_usd:.4f}" if session.run.total_cost_usd else "$0"

            text = (
                f"📊 *Progress report* — `{agent_config.id}`\n\n"
                f"Iteration: {iteration}\n"
                f"Tool calls (recent 50 msgs): {tool_calls}\n"
                f"Cost so far: {cost}\n"
                f"Status: running"
            )

            chat_id = agent_config.delivery_to
            if chat_id:
                await sender(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.debug("Progress report failed: %s", _sanitize(e))

    # ─── Force wrap-up (used by safety valve and escalation abort) ─────

    async def _force_wrapup(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        trace: Any = None,
        *,
        reason: str = "Run ending.",
    ) -> None:
        """Force the agent to produce a final summary before the run exits.

        Injects a system message with the reason, makes one final LLM call
        (with no tools so it must produce text), and records the error.
        Also stamps ``run.error_message`` so downstream (delivery,
        heartbeat reframing, analytics) can see the run didn't finish
        cleanly — otherwise a budget-exhausted beat looks "completed" to
        everyone except the one step row.
        """
        session.record_error(reason)
        # Stamp the run itself so delivery + analytics can tell this beat
        # was truncated. Don't overwrite a pre-existing error (earlier
        # failure wins).
        if not session.run.error_message:
            session.run.error_message = reason
        session.messages.append(
            {
                "role": ENGINE_CONTEXT_ROLE,
                "content": (
                    f"[SYSTEM] {reason} You MUST now produce a final summary for the user. "
                    "Describe what you accomplished and what remains to be done. "
                    "Do NOT call any tools. Do NOT start a new action in your "
                    "response (no 'Now let me...', no 'I'll send...'); this "
                    "text will be delivered verbatim as the heartbeat report."
                ),
            }
        )
        # Call with empty tool schemas so the LLM can only produce text
        await self._llm_call_and_record(
            session,
            models,
            [],
            on_content,
            broken_models,
            temperature,
            trace,
        )

        # The wrap-up call can still come back empty — provider returns blank
        # content, or only thinking blocks with no text. Without a final
        # assistant text the run's output_text is None, so a run that did
        # real work (e.g. curiosity-engine ending on a memory_block_write at
        # the iteration cap) looks like it produced nothing. Synthesize a
        # minimal summary from the tool calls so output_text is never empty
        # after work was done.
        if not (session.get_final_text() or "").strip():
            session.messages.append(
                {
                    "role": "assistant",
                    "content": self._synthesize_wrapup_summary(session, reason),
                }
            )

    @staticmethod
    def _synthesize_wrapup_summary(session: AgentSession, reason: str) -> str:
        """Build a fallback final summary when the wrap-up call produced no text.

        Lists the distinct tool actions the run completed so a truncated run
        that did real work is not reported as empty output.
        """
        tool_names: list[str] = []
        for step in session.run.steps:
            if (
                step.step_type == StepType.TOOL_CALL
                and step.tool_name
                and step.tool_name not in tool_names
            ):
                tool_names.append(step.tool_name)
        if tool_names:
            return (
                f"[Run ended: {reason}] No final summary was produced. "
                f"Completed {len(tool_names)} tool action(s): {', '.join(tool_names)}."
            )
        return f"[Run ended: {reason}] No output was produced."

    # ─── LLM call helper (shared by main loop and wrap-up) ─────

    async def _llm_call_and_record(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        trace: Any = None,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[Any, str, int, dict[str, Any]]:
        """Make an LLM call, record it in session, return (response, model, ms, msg_dict)."""
        start = time.monotonic()

        if trace:
            with trace.span("llm_call") as _span:
                response = await self._do_llm_call(
                    session,
                    models,
                    tool_schemas,
                    on_content,
                    broken_models,
                    temperature,
                    on_stream_event=on_stream_event,
                )
                # GenAI semantic-convention attributes for OTel export.
                if response is not None:
                    with contextlib.suppress(Exception):
                        from robothor.engine.telemetry import gen_ai_attributes

                        _usage = getattr(response, "usage", None)
                        _finish = ""
                        if getattr(response, "choices", None):
                            _finish = getattr(response.choices[0], "finish_reason", "") or ""
                        _span.attributes.update(
                            gen_ai_attributes(
                                model=getattr(response, "model", None)
                                or (models[0] if models else ""),
                                input_tokens=getattr(_usage, "prompt_tokens", 0) or 0,
                                output_tokens=getattr(_usage, "completion_tokens", 0) or 0,
                                finish_reason=_finish,
                            )
                        )
        else:
            response = await self._do_llm_call(
                session,
                models,
                tool_schemas,
                on_content,
                broken_models,
                temperature,
                on_stream_event=on_stream_event,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        # Touch stall watchdog — LLM responded, we're alive
        if self._active_watchdog:
            model_name = getattr(response, "model", None) or (models[0] if models else "unknown")
            self._active_watchdog.touch(f"llm_response:{model_name}")

        if response is None or not response.choices:
            return response, "", elapsed_ms, {}

        choice = response.choices[0]
        assistant_msg = choice.message
        model_used = response.model or models[0]

        # Build assistant message dict — filter thinking blocks from output text
        msg_dict: dict[str, Any] = {"role": "assistant"}
        raw_content = assistant_msg.content
        if isinstance(raw_content, list):
            # Response contains content blocks (e.g. thinking + text)
            # Keep full blocks in message for conversation continuity;
            # get_final_text() filters thinking blocks when extracting output
            msg_dict["content"] = raw_content
        else:
            if raw_content:
                msg_dict["content"] = raw_content
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]

        # Record LLM call — extract standard + cache token counts
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

        # Cache tokens: Anthropic exposes these directly; other providers
        # may use prompt_tokens_details.cached_tokens instead
        cache_creation_tokens = (
            (getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage else 0
        )
        cache_read_tokens = (getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
        if usage and not cache_read_tokens:
            details = getattr(usage, "prompt_tokens_details", None)
            if details:
                cache_read_tokens = getattr(details, "cached_tokens", 0) or 0

        session.record_llm_call(
            model=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            duration_ms=elapsed_ms,
            assistant_message=msg_dict,
        )

        # Best-effort cost tracking (cache-aware fallback)
        cost = self._response_cost(
            response=response,
            model_used=model_used,
            models=models,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        session.run.total_cost_usd += cost

        # Record step cost for projection (used by hard budget pre-flight check)
        session.record_step_cost(cost or 0.0)

        return response, model_used, elapsed_ms, msg_dict

    # ── LLM dispatch / cost — extracted to llm_client.LLMClient (Slice 1) ──
    # Thin delegators preserve the historical AgentRunner method surface for
    # existing call sites and tests; implementations live on ``self._llm``.

    def _response_cost(self, **kwargs: Any) -> float:
        return self._llm._response_cost(**kwargs)

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        return self._llm._calculate_cost(
            model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
        )

    async def _do_llm_call(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
        return await self._llm._do_llm_call(
            session,
            models,
            tool_schemas,
            on_content,
            broken_models,
            temperature,
            on_stream_event=on_stream_event,
        )

    # ─── Error Recovery Helper ──────────────────────────────────────

    async def _spawn_recovery_helper(
        self,
        agent_config: AgentConfig,
        session: AgentSession,
        action: Any,
        spawn_context: SpawnContext | None = None,
        trace: Any = None,
    ) -> str | None:
        """Spawn a helper agent to diagnose/fix an error. Returns helper output or None."""
        try:
            from robothor.engine.config import load_agent_config as _load_cfg
            from robothor.engine.models import DeliveryMode, TriggerType
            from robothor.engine.tools import _current_spawn_context

            ctx = _current_spawn_context.get()
            if ctx is None:
                logger.debug("No spawn context — cannot spawn recovery helper")
                return None

            helper_agent_id = action.agent_id or "main"
            child_config = _load_cfg(helper_agent_id, self.config.manifest_dir)
            if child_config is None:
                logger.debug("Recovery helper config not found: %s", helper_agent_id)
                return None

            # Safety: force delivery off, cap iterations, prevent deep nesting.
            # No wall-clock cap — recovery helpers run as long as they need.
            child_config.delivery_mode = DeliveryMode.NONE
            child_config.max_iterations = min(child_config.max_iterations, 5)
            child_depth = ctx.nesting_depth + 1
            if child_depth >= ctx.max_nesting_depth:
                child_config.can_spawn_agents = False

            child_ctx = SpawnContext(
                parent_run_id=ctx.parent_run_id,
                parent_agent_id=agent_config.id,
                correlation_id=ctx.correlation_id,
                nesting_depth=child_depth,
                max_nesting_depth=ctx.max_nesting_depth,
                max_spawn_batch=ctx.max_spawn_batch,
                remaining_token_budget=ctx.remaining_token_budget,
                remaining_cost_budget_usd=ctx.remaining_cost_budget_usd,
                parent_trace_id=ctx.parent_trace_id,
                parent_span_id=ctx.parent_span_id,
                person_id=getattr(ctx, "person_id", None),
            )

            run = await self.execute(
                agent_id=helper_agent_id,
                message=action.message,
                trigger_type=TriggerType.SUB_AGENT,
                trigger_detail=f"recovery_helper:{agent_config.id}",
                correlation_id=ctx.correlation_id,
                agent_config=child_config,
                spawn_context=child_ctx,
            )

            if run.error_message:
                logger.debug("Recovery helper failed: %s", run.error_message)
                return None
            return run.output_text or ""
        except Exception as e:
            logger.debug("Failed to spawn recovery helper: %s", _sanitize(e))
            return None

    # ─── v2 Enhancement Helpers ───────────────────────────────────────

    def _apply_routing(self, agent_config: AgentConfig, message: str, tool_count: int) -> Any:
        """Apply difficulty-aware routing. Returns RouteConfig or None."""
        try:
            from robothor.engine.router import get_route_config

            return get_route_config(
                message,
                tool_count,
                manual_override=agent_config.difficulty_class,
            )
        except Exception as e:
            logger.debug("Routing failed: %s", _sanitize(e))
            return None

    def _should_plan(self, agent_config: AgentConfig, route: Any) -> bool:
        """Determine if planning phase should run."""
        if agent_config.planning_enabled:
            return True
        return bool(route and route.planning is True)

    async def _run_planner(
        self,
        agent_config: AgentConfig,
        message: str,
        tool_names: list[str],
        models: list[str],
    ) -> Any:
        """Run the planning phase. Returns PlanResult or None."""
        try:
            from robothor.engine.planner import generate_plan

            plan_model = agent_config.planning_model or models[0]
            return await generate_plan(
                message,
                tool_names,
                plan_model,
                fallback_models=models[1:2],
            )
        except Exception as e:
            logger.debug("Planning phase failed: %s", _sanitize(e))
            return None

    def _create_trace(
        self,
        agent_config: AgentConfig,
        session: AgentSession,
        spawn_context: SpawnContext | None = None,
    ) -> Any:
        """Create telemetry TraceContext."""
        try:
            from robothor.engine.telemetry import TraceContext

            kwargs: dict[str, Any] = {
                "run_id": session.run_id,
                "agent_id": agent_config.id,
            }
            # Reuse parent's trace_id for unified cross-run traces
            if spawn_context and spawn_context.parent_trace_id:
                kwargs["trace_id"] = spawn_context.parent_trace_id
                kwargs["parent_trace_id"] = spawn_context.parent_trace_id
                kwargs["parent_span_id"] = spawn_context.parent_span_id

            return TraceContext(**kwargs)
        except Exception as e:
            logger.warning("Failed to create trace context: %s", e)
            return None

    def _create_scratchpad(
        self,
        agent_config: AgentConfig,
        route: Any,
        resumed_scratchpad: Any = None,
    ) -> Any:
        """Create Scratchpad if enabled."""
        enabled = agent_config.scratchpad_enabled
        if route and route.scratchpad is not None:
            enabled = route.scratchpad
        if not enabled:
            return None
        if resumed_scratchpad:
            return resumed_scratchpad
        try:
            from robothor.engine.scratchpad import Scratchpad

            return Scratchpad()
        except Exception as e:
            logger.warning("Failed to create scratchpad: %s", e)
            return None

    def _create_escalation(self, agent_config: AgentConfig) -> Any:
        """Create EscalationManager if error_feedback is enabled."""
        if not agent_config.error_feedback:
            return None
        try:
            from robothor.engine.escalation import EscalationManager

            return EscalationManager()
        except Exception:
            return None

    def _create_checkpoint(
        self,
        agent_config: AgentConfig,
        route: Any,
        run_id: str,
    ) -> Any:
        """Create CheckpointManager if enabled."""
        enabled = agent_config.checkpoint_enabled
        if route and route.checkpoint is not None:
            enabled = route.checkpoint
        if not enabled:
            return None
        try:
            from robothor.engine.checkpoint import CheckpointManager

            return CheckpointManager(run_id=run_id)
        except Exception as e:
            logger.warning("Failed to create checkpoint manager: %s", e)
            return None

    def _create_guardrails(self, agent_config: AgentConfig) -> Any:
        """Create GuardrailEngine with default + agent-specific policies."""
        try:
            import re as _re

            from robothor.engine.guardrails import GuardrailEngine, compute_effective_guardrails

            effective = compute_effective_guardrails(
                agent_config.guardrails,
                opt_out=agent_config.guardrails_opt_out,
            )
            if not effective:
                return None

            engine = GuardrailEngine(
                enabled_policies=effective,
                workspace=str(self.config.workspace) + "/",
            )
            if agent_config.exec_allowlist:
                engine._exec_allowlists[agent_config.id] = [
                    _re.compile(p) for p in agent_config.exec_allowlist
                ]
            if agent_config.write_path_allowlist:
                engine._write_allowlists[agent_config.id] = agent_config.write_path_allowlist
            if agent_config.human_approval_tools:
                engine.set_human_approval_patterns(
                    agent_config.id, agent_config.human_approval_tools
                )
            return engine
        except Exception as e:
            logger.warning("Failed to create guardrails engine: %s", e)
            return None

    def _should_verify(
        self,
        agent_config: AgentConfig,
        route: Any,
        session: AgentSession | None = None,
    ) -> bool:
        """Determine if verification step should run."""
        if agent_config.verification_enabled:
            return True
        # Skip verification for interactive sessions (adds latency, Qwen JSON unreliable)
        if (
            session
            and session.run
            and session.run.trigger_type
            in (
                TriggerType.TELEGRAM,
                TriggerType.WEBCHAT,
            )
        ):
            return False
        # Skip verification for heartbeat (scout) runs — the scout is a
        # deterministic scan-and-file beat with a rigid 5-section digest
        # format. Verification second-guesses the digest, provokes the model
        # into writing a meta-defense, and that defense overwrites the real
        # output_text — operator ends up seeing garbage instead of the digest.
        if (
            session
            and session.run
            and session.run.trigger_detail
            and session.run.trigger_detail.startswith("heartbeat:")
        ):
            return False
        return bool(route and route.verification is True)

    async def _run_verification(
        self,
        agent_config: AgentConfig,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        output_text: str | None,
        on_content: Callable[[str], Awaitable[None]] | None,
        on_tool: Callable[[dict[str, Any]], Awaitable[None]] | None,
        **loop_kwargs: Any,
    ) -> str | None:
        """Run verification step. If it fails, retry once."""
        try:
            from robothor.engine.verifier import (
                format_verification_feedback,
                verify_output,
            )

            error_count = sum(1 for s in session.run.steps if s.error_message)
            result = await verify_output(
                output_text or "",
                agent_config.verification_prompt,
                error_count,
                models[0],
                fallback_models=models[1:],
            )
            if result.passed:
                return output_text

            # Verification failed — inject feedback and retry once
            feedback = format_verification_feedback(result)
            session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": feedback})
            logger.info("Verification failed, retrying once")

            await self._run_loop(
                session,
                models,
                tool_schemas,
                agent_config,
                on_content,
                on_tool,
                **loop_kwargs,
            )
            return session.get_final_text()
        except Exception as e:
            logger.debug("Verification failed: %s", _sanitize(e))
            return output_text

    def _resume_from_checkpoint(
        self,
        run_id: str,
        session: AgentSession,
    ) -> Any:
        """Resume from a previous run's checkpoint. Returns restored scratchpad or None."""
        try:
            from robothor.engine.checkpoint import CheckpointManager
            from robothor.engine.scratchpad import Scratchpad

            checkpoint_data = CheckpointManager.load_latest(run_id)
            if not checkpoint_data:
                logger.info("No checkpoint found for run %s", run_id)
                return None

            # Restore messages
            messages = checkpoint_data.get("messages")
            if messages and isinstance(messages, list):
                session.messages = messages

            # Restore scratchpad
            scratchpad_data = checkpoint_data.get("scratchpad")
            todo_data: dict[str, Any] | None = None
            if scratchpad_data and isinstance(scratchpad_data, dict):
                # Phase 5: extract embedded TodoList before scratchpad rebuild.
                todo_data = scratchpad_data.pop("_todo_list", None)

            # Phase 5: rebuild the in-conversation TodoList from the saved
            # snapshot. Without this, the checklist that drove the run was
            # silently lost on resume — agents would lose visible progress
            # tracking mid-run and the reminder cadence would reset.
            if todo_data and isinstance(todo_data, dict) and session is not None:
                try:
                    from robothor.engine.todolist import TodoList

                    restored = TodoList.from_dict(todo_data)
                    session.todo_list = restored
                    logger.info(
                        "checkpoint.resume.todo run_id=%s items=%d",
                        run_id,
                        len(restored.items),
                        extra={
                            "event": "checkpoint.resume.todo",
                            "run_id": run_id,
                            "items_count": len(restored.items),
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Failed to restore todo_list from checkpoint: %s", _sanitize(e))

            if scratchpad_data and isinstance(scratchpad_data, dict):
                return Scratchpad.from_dict(scratchpad_data)

            return None
        except Exception as e:
            logger.warning("Failed to resume from checkpoint: %s", _sanitize(e))
            return None

    # ─── LLM Call Methods ────────────────────────────────────────────

    async def _prepare_llm_call(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        broken_models: set[str] | None = None,
    ) -> int:
        return await self._llm._prepare_llm_call(messages, models, broken_models)

    # Pure-static helpers — aliased to the extracted implementations so
    # ``AgentRunner._validate_tool_pairs(...)`` etc. keep resolving.
    _validate_tool_pairs = staticmethod(LLMClient._validate_tool_pairs)
    _guard_trailing_assistant = staticmethod(LLMClient._guard_trailing_assistant)
    _build_llm_kwargs = staticmethod(LLMClient._build_llm_kwargs)

    # Pure-static helper — aliased to the extracted implementation.
    _handle_model_error = staticmethod(LLMClient._handle_model_error)

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        return await self._llm._call_llm(
            messages, models, tools, broken_models=broken_models, temperature=temperature
        )

    async def _call_llm_streaming(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None = None,
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
        on_stream_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
        return await self._llm._call_llm_streaming(
            messages,
            models,
            tools,
            on_content,
            broken_models=broken_models,
            temperature=temperature,
            on_stream_event=on_stream_event,
        )

    @staticmethod
    def _check_primary_model_reached(run: AgentRun, agent_config: Any) -> None:
        """Alert when a run answered on a fallback instead of the configured primary.

        This is the single highest-leverage degradation detector: it turns an
        invisible, fleet-wide silent fallback (codex-not-on-PATH, 2026-05-29)
        into a per-run WARN + operator alert. Only fires for top-level runs that
        actually produced a model answer and whose used model differs from the
        manifest's primary.
        """
        if agent_config is None or getattr(run, "parent_run_id", None):
            return
        primary = getattr(agent_config, "model_primary", "") or ""
        used = run.model_used or ""
        if not primary or not used:
            return
        if _normalize_model_id(used) == _normalize_model_id(primary):
            return

        logger.error(
            "DEGRADED model: agent=%s ran on %s, not configured primary %s "
            "— primary_model_unreached=True",
            _sanitize(run.agent_id),
            _sanitize(used),
            _sanitize(primary),
        )
        note = f"Ran on fallback {used}, not primary {primary}"
        run.outcome_notes = f"{run.outcome_notes}; {note}" if run.outcome_notes else note

        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop()
            from robothor.engine.alerts import alert as _alert
            from robothor.engine.task_registry import get_task_registry

            get_task_registry().spawn(
                _alert(
                    "warning",
                    f"Primary model unreached: {run.agent_id}",
                    f"Agent `{run.agent_id}` completed on fallback `{used}` instead of "
                    f"its configured primary `{primary}`. The primary may be "
                    f"misconfigured or unavailable.",
                    metadata={
                        "agent_id": run.agent_id,
                        "model_used": used,
                        "model_primary": primary,
                    },
                ),
                name=f"primary-unreached-alert:{run.agent_id}",
            )

    @staticmethod
    def _publish_run_telemetry(trace: Any, run: AgentRun) -> None:
        """Emit run-level cache-hit-rate + token metrics (PR 4, observe-only).

        Computes the run's cumulative cache_read/cache_creation/prompt tokens
        and the derived cache_hit_ratio (cache_read / max(prompt_tokens, 1)),
        then:
        - emits them as GenAI semantic-convention attributes on a small
          ``run_summary`` span so they flow through the existing OTLP export
          (``TraceContext.build_otlp_payload`` serializes ``trace.spans``);
        - forwards the same numbers to ``trace.publish_metrics`` (Redis event
          bus), extending the run_data dict that already carries duration_ms/
          status/token counts — no new infra, no DB migration.

        Best-effort: telemetry must never break a completed run.
        """
        if not trace:
            return
        with contextlib.suppress(Exception):
            from robothor.engine.telemetry import cache_hit_ratio, gen_ai_attributes

            hit_ratio = cache_hit_ratio(run.cache_read_tokens, run.input_tokens)
            with trace.span(
                "run_summary",
                **gen_ai_attributes(
                    model=run.model_used or "",
                    input_tokens=run.input_tokens,
                    output_tokens=run.output_tokens,
                    cache_read_tokens=run.cache_read_tokens,
                    cache_creation_tokens=run.cache_creation_tokens,
                ),
            ):
                pass

            trace.publish_metrics(
                {
                    "status": "completed",
                    "duration_ms": run.duration_ms or 0,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "cache_creation_tokens": run.cache_creation_tokens,
                    "cache_read_tokens": run.cache_read_tokens,
                    "cache_hit_ratio": hit_ratio,
                }
            )

    def _finish_run(
        self,
        run: AgentRun,
        trace: Any = None,
        agent_config: Any = None,
        session: Any = None,
        spawn_context: Any = None,
    ) -> AgentRun:
        """Finalize run and spawn background DB persistence.

        Returns the AgentRun immediately — DB writes happen asynchronously
        so callers (especially interactive Telegram sessions) are not blocked.

        agent_config is optional — when passed, post-run guardrails
        (e.g. requires_human_task_closure) will run against the finished run.
        """
        # ── [GUARDRAIL] Post-run checks (require finished run + config) ──
        if agent_config is not None:
            try:
                from robothor.engine.guardrails import check_post_run

                check_post_run(run, agent_config, tenant_id=getattr(run, "tenant_id", ""))
            except Exception as e:
                logger.warning("post-run guardrail error: %s", _sanitize(e))

        # ── [OBSERVABILITY] Configured-primary-not-reached detector ──────
        # A run that completes on a fallback model looks identical to a healthy
        # run in the DB, so a dead primary (e.g. codex/gpt-5.5 missing from the
        # engine PATH) can degrade the whole fleet silently. Emit a loud signal
        # whenever the model that actually answered isn't the configured
        # primary. Sub-agent runs inherit the parent's models, so skip them.
        with contextlib.suppress(Exception):
            self._check_primary_model_reached(run, agent_config)

        # ── Phase 0 hook: post-response extension point ──────────────
        # No-op by default; future rips spawn background reviews
        # (Rip 1), persist trajectories (Rip 10), etc. Suppressed so a
        # broken hook never blocks the run from finalizing.
        if session is not None:
            with contextlib.suppress(Exception):
                self._after_response_delivered(session, run)

        # ── [STAGE 5] Lift unfinished todo_write items to the parent task ──
        # Closes the "full circle": a worker that ran out of
        # iterations/budget with pending items writes them back to the
        # CRM parent task so the thread planner picks up next beat.
        if os.environ.get("ROBOTHOR_TODO_ESCALATE_ENABLED", "1") == "1":
            try:
                todos = getattr(session, "todo_list", None) if session else None
                parent_task_id = spawn_context.parent_task_id if spawn_context else None
                if todos and parent_task_id:
                    esc_kwargs: dict[str, Any] = {
                        "todos": todos,
                        "parent_task_id": parent_task_id,
                        "agent_id": run.agent_id,
                        "tenant_id": getattr(run, "tenant_id", "") or "",
                        "agent_config": agent_config,
                        "run_id": getattr(run, "id", None),
                    }
                    # Escalation does up to ~15 blocking psycopg2 round-trips
                    # (get_task + set_next_action + update_task + per-item
                    # promotion). Offload to a worker thread so it never blocks
                    # the event loop — same spawn-or-run-sync pattern as
                    # _persist_run below. Sync fallback for CLI/tests (no loop).
                    try:
                        asyncio.get_running_loop()
                        from robothor.engine.task_registry import get_task_registry

                        get_task_registry().spawn(
                            self._escalate_unfinished_todos_bg(esc_kwargs),
                            name=f"todo-escalate:{run.id}",
                        )
                    except RuntimeError:
                        _escalate_unfinished_todos(**esc_kwargs)
            except Exception as e:
                logger.warning("todo escalation error: %s", _sanitize(e))

        # ── [RECENT ACTIONS] Cross-session surface for tracked agents ──
        try:
            from robothor.engine.recent_actions import record_run

            record_run(run, agent_config=agent_config)
        except Exception as e:
            logger.warning("recent_actions write error: %s", _sanitize(e))

        # ── [CONTACT 360] Timeline activity emission ──
        # Best-effort: no-op when the run is not linked to a person (cron jobs
        # and the like).  Runs in the caller thread; it's a single idempotent
        # INSERT, fast enough that backgrounding isn't worth the complexity.
        try:
            from robothor.engine.run_person_link import emit_run_timeline_activity

            emit_run_timeline_activity(run)
        except Exception as e:  # noqa: BLE001
            logger.warning("timeline_activity emit error: %s", _sanitize(e))

        # ── [HOOKS] AGENT_END lifecycle hook (fire-and-forget) ──
        try:
            from robothor.engine.hook_registry import HookContext, HookEvent, get_hook_registry

            hr = get_hook_registry()
            if hr:
                end_ctx = HookContext(
                    event=HookEvent.AGENT_END,
                    agent_id=run.agent_id,
                    run_id=run.id,
                    output_text=run.output_text or "",
                    error=run.error_message or "",
                    metadata={
                        "status": run.status.value
                        if hasattr(run.status, "value")
                        else str(run.status),
                    },
                )
                try:
                    asyncio.get_running_loop()
                    from robothor.engine.task_registry import get_task_registry

                    get_task_registry().spawn(
                        hr.dispatch(HookEvent.AGENT_END, end_ctx),
                        name=f"agent-end-hook:{run.agent_id}",
                    )
                except RuntimeError:
                    pass
        except Exception as e:
            logger.warning("AGENT_END hook error: %s", _sanitize(e))

        # Spawn DB persistence as a background task so the caller gets the run back immediately.
        try:
            asyncio.get_running_loop()
            from robothor.engine.task_registry import get_task_registry

            get_task_registry().spawn(
                self._persist_run(run),
                name=f"persist-run:{run.id}",
            )
        except RuntimeError:
            # No event loop (CLI, tests) — persist synchronously
            self._persist_run_sync(run)

        return run

    async def _persist_run(self, run: AgentRun) -> None:
        """Persist run state and steps to the database in a background thread."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._persist_run_sync, run)

    async def _escalate_unfinished_todos_bg(self, kwargs: dict[str, Any]) -> None:
        """Run the blocking todo escalation/promotion off the event loop.

        Best-effort: the escalation is non-critical (the next heartbeat re-plans
        from the parent task anyway), so failures are logged, not raised.
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, functools.partial(_escalate_unfinished_todos, **kwargs)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("todo escalation (bg) error: %s", _sanitize(e))

    @staticmethod
    def _assess_outcome(run: AgentRun) -> None:
        """Assess run outcome using simple heuristics.

        All trigger types are assessed (cron, hook, workflow, interactive).
        Sub-agent runs are skipped (parent handles assessment).
        """
        from robothor.engine.models import RunStatus

        # Skip sub-agent runs
        if run.parent_run_id:
            return

        if run.status == RunStatus.TIMEOUT:
            run.outcome_assessment = "abandoned"
            run.outcome_notes = "Run timed out"
        elif run.status == RunStatus.FAILED:
            run.outcome_assessment = "incorrect"
            run.outcome_notes = f"Run failed: {(run.error_message or 'unknown')[:200]}"
        elif run.status == RunStatus.COMPLETED:
            if run.budget_exhausted:
                run.outcome_assessment = "partial"
                run.outcome_notes = "Budget exhausted before completion"
            elif run.error_message:
                run.outcome_assessment = "partial"
                run.outcome_notes = f"Completed with errors: {run.error_message[:200]}"
            elif not run.output_text or len(run.output_text.strip()) < 10:
                run.outcome_assessment = "partial"
                run.outcome_notes = "Completed with minimal output"
            elif (
                run.delivery_mode == DeliveryMode.ANNOUNCE
                and len(run.output_text.strip()) < ANNOUNCE_MIN_OUTPUT_CHARS
            ):
                run.outcome_assessment = "partial"
                run.outcome_notes = (
                    f"Thin announce output ({len(run.output_text.strip())} chars) "
                    "— likely meta-confirmation instead of full content"
                )
            else:
                run.outcome_assessment = "successful"
                run.outcome_notes = None

    def _persist_run_sync(self, run: AgentRun) -> None:
        """Synchronous DB persistence — update run + batch-insert steps + CRM task."""
        # Assess outcome for interactive runs before persisting
        self._assess_outcome(run)

        try:
            update_run(
                run.id,
                status=run.status.value,
                completed_at=run.completed_at,
                duration_ms=run.duration_ms,
                model_used=run.model_used,
                models_attempted=run.models_attempted,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                cache_creation_tokens=run.cache_creation_tokens or None,
                cache_read_tokens=run.cache_read_tokens or None,
                total_cost_usd=run.total_cost_usd,
                output_text=run.output_text,
                error_message=run.error_message,
                error_traceback=run.error_traceback,
                delivery_status=run.delivery_status,
                delivered_at=run.delivered_at,
                delivery_channel=run.delivery_channel,
                token_budget=run.token_budget or None,
                cost_budget_usd=run.cost_budget_usd or None,
                budget_exhausted=run.budget_exhausted or None,
                outcome_assessment=run.outcome_assessment,
                outcome_notes=run.outcome_notes,
            )
            # Flush only steps the session hasn't already committed —
            # per-iteration flushes (see AgentSession.flush_new_steps_sync)
            # persist along the way, so on normal completion this is
            # usually the tail (record_error + final assistant turn).
            pending = run.steps[run.persisted_step_count :]
            if pending:
                try:
                    create_steps_batch(pending)
                except Exception:
                    for step in pending:
                        try:
                            create_step(step)
                        except Exception as e:
                            logger.warning("Failed to record step: %s", _sanitize(e))
                run.persisted_step_count = len(run.steps)
        except Exception as e:
            logger.warning("Failed to update run in database: %s", _sanitize(e))

        if run.task_id:
            try:
                from robothor.crm.dal import resolve_task as dal_resolve_task
                from robothor.crm.dal import update_task as dal_update_task
                from robothor.engine.models import RunStatus

                if run.status == RunStatus.COMPLETED:
                    dal_resolve_task(
                        run.task_id,
                        resolution=f"Run completed: {(run.output_text or '')[:200]}",
                        agent_id=run.agent_id,
                    )
                elif run.status in (RunStatus.FAILED, RunStatus.TIMEOUT):
                    dal_update_task(
                        run.task_id,
                        status="TODO",
                        tags=[run.agent_id, "failed", run.status.value],
                    )
            except Exception as e:
                logger.warning("Auto-task update failed: %s", _sanitize(e))
