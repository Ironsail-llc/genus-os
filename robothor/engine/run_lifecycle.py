"""Run lifecycle scaffolding for AgentRunner — setup, pacing, recovery.

Extracted from runner.py 2026-08-24 (phase 2 of the god-object
decomposition). Covers session-object creation (trace/scratchpad/escalation/
checkpoint/guardrails), planning, routing, checkpoint resume, verification
gating, the in-run watchdog, progress reports, forced wrapup, and recovery
helper spawns. CONTRACT: ``self.config``, the LLM mixin's
``_llm_call_and_record``, ``self.execute`` (recovery helpers re-enter the
runner), and each other — nothing else. A new ``self.*`` dependency is the
god-object growing back; put it on the signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.config import EngineConfig
    from robothor.engine.session import AgentSession

import logging
import re
from typing import Any

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.models import (
    AgentConfig,
    SpawnContext,
    StepType,
    TriggerType,
)

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.session import ENGINE_CONTEXT_ROLE, AgentSession
from robothor.engine.stall_watchdog import (
    _active_watchdog_var,
    _StallWatchdog,
)

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


class RunLifecycleMixin:
    """See module docstring for the contract."""

    if TYPE_CHECKING:
        # Provided by the composed AgentRunner — the mixin contract's whole
        # allowed surface, declared so mypy checks it and nothing more.
        config: EngineConfig

        async def _llm_call_and_record(self, *args: Any, **kwargs: Any) -> Any: ...

        async def execute(self, *args: Any, **kwargs: Any) -> Any: ...

        async def _run_loop(self, *args: Any, **kwargs: Any) -> Any: ...

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
                identity=getattr(ctx, "identity", None),
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
                # The whole remaining chain, not one model: models[1:2] can
                # never reach the offline tier that terminates every chain, so
                # a cloud outage silently removed the planning stage from every
                # run at the same moment it removed the strong model.
                fallback_models=models[1:],
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
                rate_limit_per_minute=getattr(agent_config, "rate_limit_per_minute", 0),
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
                        _sanitize(run_id),
                        len(restored.items),
                        extra={
                            "event": "checkpoint.resume.todo",
                            "run_id": _sanitize(run_id),
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
