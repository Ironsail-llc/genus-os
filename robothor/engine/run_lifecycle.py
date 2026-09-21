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
    from robothor.engine.runner import AgentRunner
    from robothor.engine.session import AgentSession

import logging
from typing import Any, cast

from robothor.engine.llm_attempts import is_attempt_step

# LLM dispatch/cost/streaming + the request-timeout constants now live in
# llm_client.LLMClient (Phase A / Slice 1). AgentRunner delegates to an
# instance of it; the historical method surface is preserved via thin
# delegators/aliases below so existing call sites keep working unchanged.
from robothor.engine.models import (
    AgentConfig,
    SpawnContext,
    StepType,
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


def spawn_post_stall_autodream(agent_id: str) -> None:
    """Kick off memory consolidation after the stall watchdog kills a run.

    Best-effort and deliberately silent on failure: this is cleanup on a path
    that is already finalizing a dead run, and an autoDream import error must
    not stop the terminal row being written.

    Lives here rather than inline in ``runner.execute`` because "recovery
    helper spawns" is this module's own contract, and the runner is the
    god-object the decomposition ratchet exists to shrink.
    """
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
            from robothor.engine.tools.dispatch import ToolContext
            from robothor.engine.tools.handlers.spawn import _handle_spawn_agent

            # Recovery must use exactly the same release, target, depth,
            # identity, attempt and cancellation boundary as ordinary spawns.
            run = session.run
            result = await _handle_spawn_agent(
                {
                    "agent_id": action.agent_id or "main",
                    "message": action.message,
                    "max_iterations": 5,
                },
                ctx=ToolContext(
                    agent_id=agent_config.id,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    user_role=run.user_role,
                    is_benchmark=getattr(run, "is_benchmark", False),
                    identity=getattr(session, "identity", None),
                ),
                agent_id=agent_config.id,
                # This mixin is only ever composed into AgentRunner, which is what
                # the spawn handler needs; the mixin has no runner type of its own.
                _runner=cast("AgentRunner", self),
            )
            if result.get("error") or result.get("status") != "completed":
                logger.debug("Recovery helper refused or failed")
                return None
            return result.get("output_text") or ""
        except Exception as e:
            logger.debug("Failed to spawn recovery helper: %s", _sanitize(e))
            return None

    # ─── v2 Enhancement Helpers ───────────────────────────────────────

    def _attach_plan_context(self, session, plan_result):
        """Optional planner formatting must not abort native execution."""
        try:
            from robothor.engine.planner import format_plan_context
            from robothor.engine.session import ENGINE_CONTEXT_ROLE

            context = format_plan_context(plan_result)
            if context:
                session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": context})
            return context
        except Exception as exc:
            logger.warning("Plan context formatting failed (non-fatal): %s", _sanitize(exc))
            return ""

    def _apply_routing(self, agent_config: AgentConfig, message: str, tool_count: int) -> Any:
        """Apply difficulty-aware routing. Returns RouteConfig or None."""
        try:
            from robothor.engine.router import get_route_config
            from robothor.engine.runtime.automatic_planning import context_for

            return get_route_config(
                message,
                tool_count,
                manual_override=agent_config.difficulty_class,
                # A large available catalogue does not mean an ordinary chat
                # request needs an auxiliary planning call. Keep configured
                # planning and long/delegated/resumed work on their old policy.
                catalogue_implies_complexity=context_for(agent_config) is None,
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
            from robothor.engine.provider_routing import provider_order_scope
            from robothor.engine.runtime.automatic_planning import run as automatic_plan

            plan_model = agent_config.planning_model or models[0]
            with provider_order_scope(getattr(agent_config, "provider_order", {})):
                return await automatic_plan(
                    agent_config,
                    lambda: generate_plan(
                        message,
                        tool_names,
                        plan_model,
                        # Retain the entire configured fallback chain.
                        fallback_models=models[1:],
                    ),
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
        self, agent_config: AgentConfig, route: Any, session: AgentSession | None = None
    ) -> bool:
        from robothor.engine.verifier import should_verify

        return should_verify(agent_config, route, session)

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
            from robothor.engine.provider_routing import provider_order_scope
            from robothor.engine.verifier import (
                format_verification_feedback,
                verify_output,
            )

            # Attempt rows are excluded: a retried LLM call that the same
            # model then answered is latency, not a run error, and the default
            # criteria ("… without errors") turns one into a failed verdict
            # and a SECOND full agent loop. On event-triggered runs the
            # reasoning-only retry is the common path, not an edge.
            error_count = sum(
                1 for s in session.run.steps if s.error_message and not is_attempt_step(s)
            )
            with provider_order_scope(getattr(agent_config, "provider_order", {})):
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
            # A retry that yields nothing must not turn a delivered report
            # into nothing: the original output is still the best we have.
            return session.get_final_text() or output_text
        except Exception as e:
            logger.debug("Verification failed: %s", _sanitize(e))
            return output_text

    async def _resume_checkpoint(self, run_id: str, session: AgentSession) -> Any:
        import asyncio

        # Use the runner's persistence seam, also used when the run is created.
        from robothor.engine.runner import update_run

        scratchpad = self._resume_from_checkpoint(run_id, session)
        if not await asyncio.to_thread(update_run, session.run.id, task_text=session.run.task_text):
            raise RuntimeError("Failed to persist restored task before continuation")
        return scratchpad

    def _resume_from_checkpoint(
        self,
        run_id: str,
        session: AgentSession,
    ) -> Any:
        """Restore a checkpoint or fail closed; only the scratchpad is optional."""
        try:
            from robothor.engine.checkpoint import CheckpointManager
            from robothor.engine.scratchpad import Scratchpad

            checkpoint_data = CheckpointManager.load_latest(run_id, tenant_id=session.run.tenant_id)
            if not checkpoint_data:
                raise ValueError("checkpoint unavailable; refusing fresh execution")

            # Restore messages
            messages = checkpoint_data.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("checkpoint conversation is missing or malformed")
            if messages and isinstance(messages, list):
                session.messages = messages
                from robothor.engine.task_context import install_context, make_context, read_context

                record = read_context(messages)
                if record is None:
                    record = make_context(
                        session.run.task_text or session.originating_message, [], run_id=run_id
                    )
                    install_context(session.messages, record)
                session.originating_message = record["request"]
                from robothor.engine.deliverable_contract import task_text_for_column

                session.run.task_text = task_text_for_column(record["request"])

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
            raise RuntimeError("Failed to restore checkpoint; reconcile before retrying") from e

    # ─── LLM Call Methods ────────────────────────────────────────────
