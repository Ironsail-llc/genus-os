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
from robothor.engine.models import (
    AgentConfig,
    AgentRun,
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
from robothor.engine.session import AgentSession
from robothor.engine.tools import get_registry
from robothor.engine.tracking import create_run, create_step, update_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True

# Register custom pricing for OpenRouter-routed models.
# Without this, litellm.completion_cost() returns $0.00 for these models.
# NOTE: max_tokens here is the MODEL's context window, not the output cap we request.
litellm.register_model(
    {
        "openrouter/z-ai/glm-5": {
            "max_tokens": 204800,
            "input_cost_per_token": 0.0000008,  # $0.80/M
            "output_cost_per_token": 0.00000256,  # $2.56/M
        },
        "openrouter/anthropic/claude-sonnet-4.6": {
            "max_tokens": 200000,
            "input_cost_per_token": 0.000003,  # $3/M
            "output_cost_per_token": 0.000015,  # $15/M
        },
    }
)


class AgentRunner:
    """Executes agents: builds prompt, enters tool loop, tracks everything."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.registry = get_registry()

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
        model_override: str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        resume_from_run_id: str | None = None,
        spawn_context: SpawnContext | None = None,
        readonly_mode: bool = False,
        execution_mode: bool = False,
        deep_plan: bool = False,
    ) -> AgentRun:
        """Execute an agent with the given message.

        Args:
            execution_mode: When True, prepend EXECUTION_MODE_PREAMBLE to
                system prompt to enforce plan execution (no re-planning).
        Returns the completed AgentRun with full metadata.
        """
        # Load agent config from manifest if not provided
        if agent_config is None:
            agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if agent_config is None:
            logger.error("Agent config not found: %s", agent_id)
            session = AgentSession(agent_id, trigger_type, trigger_detail, self.config.tenant_id)
            session.start("", message, [])
            return session.fail(f"Agent config not found: {agent_id}")

        # Create session
        session = AgentSession(
            agent_id=agent_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            tenant_id=self.config.tenant_id,
            correlation_id=correlation_id,
        )

        # Sub-agent: link to parent run
        if spawn_context:
            session.run.parent_run_id = spawn_context.parent_run_id
            session.run.nesting_depth = spawn_context.nesting_depth + 1

        # Build system prompt + warmup in parallel where possible.
        # Both involve sync I/O so we run them concurrently in the executor.
        loop = asyncio.get_running_loop()
        t_setup_start = time.monotonic()

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

        # Launch system prompt build + warmup concurrently
        sys_prompt_future = loop.run_in_executor(
            None, build_system_prompt, agent_config, self.config.workspace
        )

        warmup_future: asyncio.Future[str | None] | None = None
        if warmup_kind == "cron":
            from robothor.engine.warmup import build_warmth_preamble

            warmup_future = loop.run_in_executor(
                None,
                lambda: build_warmth_preamble(
                    agent_config, self.config.workspace, self.config.tenant_id
                ),
            )
        elif warmup_kind == "interactive":
            from robothor.engine.warmup import build_interactive_preamble

            warmup_future = loop.run_in_executor(
                None,
                lambda: build_interactive_preamble(agent_id, message, include_blocks=True),
            )

        # Await both concurrently
        system_prompt = await sys_prompt_future
        warmup_preamble: str | None = None
        if warmup_future is not None:
            try:
                warmup_preamble = await warmup_future
            except Exception as e:
                logger.debug("Warmup preamble failed for %s: %s", agent_id, e)

        if warmup_preamble:
            message = f"{warmup_preamble}\n\n{message}"

        t_setup_ms = int((time.monotonic() - t_setup_start) * 1000)
        logger.info(
            "SETUP %dms agent=%s trigger=%s warmup=%s cached_prompt=%s",
            t_setup_ms,
            agent_id,
            trigger_type.value,
            warmup_kind or "none",
            "hit" if _prompt_cache.get(agent_config.id) else "miss",
        )

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

        # Execute with timeout — covers EVERYTHING: DB init, warmup, planner, run loop.
        # Previously only _run_loop was wrapped, so hangs during initialization
        # (DB pool exhaustion, warmup blocking) bypassed the timeout entirely.
        timeout = agent_config.timeout_seconds
        trace = None  # initialized inside timeout block, but referenced in except handlers
        try:
            async with asyncio.timeout(timeout):
                # Record run in database (sync DB call — run in executor to avoid blocking event loop)
                try:
                    await asyncio.get_running_loop().run_in_executor(None, create_run, session.run)
                except Exception as e:
                    logger.warning("Failed to record run start: %s", e)

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
                        logger.warning("Auto-task creation failed: %s", e)

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
                    return self._finish_run(session.fail("No models configured"))

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
                            session.messages.append({"role": "user", "content": plan_context})

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
                    on_status=on_status,
                )
        except TimeoutError:
            logger.warning("Agent %s timed out after %ds", agent_id, timeout)
            session.record_error(f"Timed out after {timeout}s")
            return self._finish_run(session.timeout(), trace=trace)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("Agent %s failed: %s", agent_id, e, exc_info=True)
            session.record_error(str(e), tb)
            return self._finish_run(session.fail(str(e), tb), trace=trace)

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
            )

        # ── [TELEMETRY] Publish run metrics ──
        if trace:
            with contextlib.suppress(Exception):
                trace.publish_metrics(
                    {
                        "status": "completed",
                        "duration_ms": session.run.duration_ms or 0,
                        "input_tokens": session.run.input_tokens,
                        "output_tokens": session.run.output_tokens,
                    }
                )

        return self._finish_run(session.complete(output_text), trace=trace)

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
            logger.warning("Failed to record deep run start: %s", e)

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
            logger.error("execute_deep failed: %s", e, exc_info=True)
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
        on_status: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
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
                remaining_token_budget=session.run.token_budget,
                parent_trace_id=trace.trace_id if trace else "",
                parent_span_id="",
            )
            _current_spawn_context.set(fresh_ctx)

        # ── v2: Initialize enhancement objects ──
        scratchpad = self._create_scratchpad(agent_config, route, resumed_scratchpad)
        escalation = self._create_escalation(agent_config)
        checkpoint = self._create_checkpoint(agent_config, route, session.run_id)
        guardrail_engine = self._create_guardrails(agent_config)

        # Wire plan into scratchpad for progress tracking
        if scratchpad and plan_result and hasattr(plan_result, "plan") and plan_result.plan:
            scratchpad.set_plan(plan_result.plan)

        budget_warning_sent = False
        plan_steps = 0
        if plan_result and hasattr(plan_result, "estimated_steps"):
            plan_steps = plan_result.estimated_steps

        # Soft check-in interval (repurposed from old max_iterations hard cap)
        _checkin_interval = max_iterations
        _safety_cap = getattr(agent_config, "safety_cap", 200)
        _iteration = 0

        while True:
            # ── [SAFETY VALVE] Absolute iteration cap (infinite-loop protection) ──
            if _iteration >= _safety_cap:
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
                        "role": "user",
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

            # ── [BUDGET] Token tracking (informational only, not enforced) ──
            budget_status = session.check_budget(session.run.token_budget)
            if budget_status == "exhausted" and not session.run.budget_exhausted:
                session.run.budget_exhausted = True  # track for analytics
            if budget_status == "warning" and not budget_warning_sent:
                budget_warning_sent = True
                session.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[SYSTEM] Token usage note: you have used >80% of the "
                            "estimated token budget for this run. This is informational "
                            "only — continue working as needed to complete the task."
                        ),
                    }
                )

            # ── [SCRATCHPAD] Inject working state summary ──
            if scratchpad and scratchpad.should_inject():
                summary = scratchpad.format_summary(plan_steps=plan_steps)
                session.messages.append({"role": "user", "content": summary})

            # ── LLM call ──
            response, model_used, elapsed_ms, msg_dict = await self._llm_call_and_record(
                session,
                models,
                tool_schemas,
                on_content,
                broken_models,
                agent_config.temperature,
                trace,
            )

            if response is None:
                session.record_error("All models failed")
                raise RuntimeError("All models failed to respond")

            if not response.choices:
                session.record_error("LLM returned empty choices")
                raise RuntimeError("LLM returned empty choices")

            assistant_msg = response.choices[0].message

            # Check if we're done (no tool calls)
            if not assistant_msg.tool_calls:
                # In plan mode, nudge the agent to research if it skipped tools
                # on the very first iteration (only fires once).
                if readonly_mode and _iteration == 0:
                    session.messages.append(
                        {
                            "role": "user",
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

                # ── [GUARDRAILS] Pre-execution check ──
                if guardrail_engine:
                    gr = guardrail_engine.check_pre_execution(
                        tool_name, tool_args, agent_id=agent_config.id
                    )
                    if not gr.allowed:
                        gr_error_msg = f"Blocked by guardrail ({gr.guardrail_name}): {gr.reason}"
                        session.record_tool_call(
                            tool_name=tool_name,
                            tool_input=tool_args,
                            tool_output={"error": gr_error_msg, "guardrail": gr.guardrail_name},
                            tool_call_id=tc.id,
                            error_message=gr_error_msg,
                        )
                        iteration_errors.append((tool_name, gr_error_msg, None))
                        with contextlib.suppress(Exception):
                            from robothor.engine.tracking import log_tool_event

                            log_tool_event(
                                run_id=session.run.id,
                                tool_name=tool_name,
                                duration_ms=0,
                                success=False,
                                error_type="guardrail_blocked",
                            )
                        if scratchpad:
                            scratchpad.record_tool_call(tool_name, error=gr_error_msg)
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
                if trace:
                    with trace.span("tool_call", tool=tool_name) as _span:
                        result = await self.registry.execute(
                            tool_name,
                            tool_args,
                            agent_id=agent_config.id,
                            tenant_id=session.run.tenant_id,
                            workspace=str(self.config.workspace),
                        )
                else:
                    result = await self.registry.execute(
                        tool_name,
                        tool_args,
                        agent_id=agent_config.id,
                        tenant_id=session.run.tenant_id,
                        workspace=str(self.config.workspace),
                    )
                tool_elapsed = int((time.monotonic() - tool_start) * 1000)

                error_msg: str | None = result.get("error") if isinstance(result, dict) else None

                # ── [GUARDRAILS] Post-execution check ──
                if guardrail_engine and not error_msg:
                    post_gr = guardrail_engine.check_post_execution(tool_name, result)
                    if post_gr.action == "warned":
                        logger.warning("Guardrail warning for %s: %s", tool_name, post_gr.reason)

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
                    except Exception:
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

                session.record_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_args,
                    tool_output=result,
                    tool_call_id=tc.id,
                    duration_ms=tool_elapsed,
                    error_message=error_msg,
                )

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

                # ── [ESCALATION] Record error/success ──
                if escalation:
                    if error_msg:
                        from robothor.engine.models import ErrorType

                        escalation.record_error(error_type or ErrorType.UNKNOWN)
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
                                "role": "user",
                                "content": f"[SYSTEM] {action.message} Retrying now.",
                            }
                        )
                        recovery_applied = True

                    elif action.action == "retry":
                        session.messages.append(
                            {
                                "role": "user",
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
                                    "role": "user",
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
                                "role": "user",
                                "content": f"[SYSTEM — Recovery guidance] {action.message}",
                            }
                        )
                        recovery_applied = True

            # ── [ERROR FEEDBACK] Inject analysis prompt on errors ──
            if iteration_errors and agent_config.error_feedback and not recovery_applied:
                error_lines = "\n".join(
                    f"- {name}: {msg}" for name, msg, _etype in iteration_errors
                )
                session.messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[SYSTEM] The following tool calls failed:\n{error_lines}\n\n"
                            "Analyze why these failed. Consider:\n"
                            "1. Were the arguments correct?\n"
                            "2. Is there an alternative approach or different tool?\n"
                            "3. Should you skip this step and continue?\n"
                            "Do NOT retry the exact same call with the same arguments."
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
                    session.messages.append({"role": "user", "content": esc_msg})

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
                        session.messages.append(
                            {
                                "role": "user",
                                "content": f"[REVISED PLAN — attempt {_replan_count}]\n{plan_context}",
                            }
                        )

            # ── [CHECKPOINT] Save state ──
            if checkpoint and checkpoint.should_checkpoint():
                checkpoint.save(
                    step_number=session._step_counter,
                    messages=session.messages,
                    scratchpad=scratchpad.to_dict() if scratchpad else None,
                    plan=plan_result.raw if plan_result and hasattr(plan_result, "raw") else None,
                )

            _iteration += 1

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
        """
        session.record_error(reason)
        session.messages.append(
            {
                "role": "user",
                "content": (
                    f"[SYSTEM] {reason} You MUST now produce a final summary for the user. "
                    "Describe what you accomplished and what remains to be done. "
                    "Do NOT call any tools."
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
                )
        else:
            response = await self._do_llm_call(
                session,
                models,
                tool_schemas,
                on_content,
                broken_models,
                temperature,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

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

        # Record LLM call
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
        session.record_llm_call(
            model=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=elapsed_ms,
            assistant_message=msg_dict,
        )

        # Best-effort cost tracking
        try:
            cost = litellm.completion_cost(completion_response=response, model=models[0])
            if cost and cost > 0:
                session.run.total_cost_usd += cost
            else:
                cost = self._calculate_cost(models[0], input_tokens, output_tokens)
                session.run.total_cost_usd += cost
        except Exception:
            cost = self._calculate_cost(models[0], input_tokens, output_tokens)
            session.run.total_cost_usd += cost

        return response, model_used, elapsed_ms, msg_dict

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost from litellm model registry."""
        info = litellm.model_cost.get(model, {})
        input_cost: float = info.get("input_cost_per_token", 0.0)
        output_cost: float = info.get("output_cost_per_token", 0.0)
        return input_tokens * input_cost + output_tokens * output_cost

    async def _do_llm_call(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
    ) -> Any:
        """Dispatch to streaming or non-streaming LLM call."""
        if on_content:
            return await self._call_llm_streaming(
                session.messages,
                models,
                tool_schemas,
                on_content,
                broken_models=broken_models,
                temperature=temperature,
            )
        return await self._call_llm(
            session.messages,
            models,
            tool_schemas,
            broken_models=broken_models,
            temperature=temperature,
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

            # Safety: force delivery off, cap iterations/timeout, prevent deep nesting
            child_config.delivery_mode = DeliveryMode.NONE
            child_config.max_iterations = min(child_config.max_iterations, 5)
            child_config.timeout_seconds = min(child_config.timeout_seconds, 60)
            child_depth = ctx.nesting_depth + 1
            if child_depth >= ctx.max_nesting_depth:
                child_config.can_spawn_agents = False

            child_ctx = SpawnContext(
                parent_run_id=ctx.parent_run_id,
                parent_agent_id=agent_config.id,
                correlation_id=ctx.correlation_id,
                nesting_depth=child_depth,
                max_nesting_depth=ctx.max_nesting_depth,
                remaining_token_budget=ctx.remaining_token_budget,
                remaining_cost_budget_usd=ctx.remaining_cost_budget_usd,
                parent_trace_id=ctx.parent_trace_id,
                parent_span_id=ctx.parent_span_id,
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
            logger.debug("Failed to spawn recovery helper: %s", e)
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
            logger.debug("Routing failed: %s", e)
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
            logger.debug("Planning phase failed: %s", e)
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
        except Exception:
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
        except Exception:
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
        except Exception:
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
            return engine
        except Exception:
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
            session.messages.append({"role": "user", "content": feedback})
            logger.info("Verification failed for %s, retrying once", agent_config.id)

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
            logger.debug("Verification failed: %s", e)
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
            if scratchpad_data and isinstance(scratchpad_data, dict):
                return Scratchpad.from_dict(scratchpad_data)

            return None
        except Exception as e:
            logger.warning("Failed to resume from checkpoint: %s", e)
            return None

    # ─── LLM Call Methods ────────────────────────────────────────────

    async def _prepare_llm_call(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
    ) -> int:
        """Shared pre-flight: compress context and estimate input tokens.

        Mutates messages in-place. Returns estimated input token count.
        """
        from robothor.engine.context import estimate_tokens, maybe_compress
        from robothor.engine.model_registry import get_model_limits

        try:
            model_limits = get_model_limits(models[0])
            compress_threshold = int(model_limits.max_input_tokens * 0.75)
            messages[:] = await maybe_compress(messages, models, threshold=compress_threshold)
        except Exception as e:
            logger.debug("Pre-flight compression failed: %s", e)

        return estimate_tokens(messages)

    @staticmethod
    def _build_llm_kwargs(
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        input_est: int,
        temperature: float,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build kwargs dict for litellm.acompletion."""
        from robothor.engine.model_registry import get_model_limits, get_output_tokens

        limits = get_model_limits(model)
        actual_model = model

        kwargs: dict[str, Any] = {
            "model": actual_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": get_output_tokens(model, input_est),
            "timeout": 180 if model.startswith("ollama_chat/") else 120,
        }
        if stream:
            kwargs["stream"] = True
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if limits.supports_thinking:
            from robothor.engine.model_registry import THINKING_BUDGET_TOKENS

            kwargs["temperature"] = 1.0  # Required by Anthropic API
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": THINKING_BUDGET_TOKENS,
            }
        return kwargs

    @staticmethod
    def _handle_model_error(
        e: Exception,
        model: str,
        broken_models: set[str] | None,
        *,
        streaming: bool = False,
    ) -> None:
        """Handle model failure: mark broken or log warning."""
        status = getattr(e, "status_code", None)
        if broken_models is not None and status in (401, 403, 429):
            broken_models.add(model)
            logger.warning(
                "Model %s permanently failed (%s), removing from rotation",
                model,
                status,
            )
        else:
            suffix = " (streaming)" if streaming else ""
            logger.warning("Model %s%s failed: %s", model, suffix, e)

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        """Call LLM with model fallback. Returns litellm response or None."""
        input_est = await self._prepare_llm_call(messages, models)
        last_error = None

        logger.debug("LLM call with models: %s (broken: %s)", models, broken_models or set())
        for model in models:
            if broken_models and model in broken_models:
                continue
            try:
                kwargs = self._build_llm_kwargs(model, messages, tools, input_est, temperature)
                return await litellm.acompletion(**kwargs)
            except Exception as e:
                self._handle_model_error(e, model, broken_models)
                last_error = e

        logger.error(
            "All models failed. Models: %s, broken: %s, last error: %s",
            models,
            broken_models or set(),
            last_error,
        )
        return None

    async def _call_llm_streaming(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict[str, Any]],
        on_content: Callable[[str], Awaitable[None]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        """Call LLM with streaming. Returns reconstructed ModelResponse."""
        input_est = await self._prepare_llm_call(messages, models)
        last_error = None

        for model in models:
            if broken_models and model in broken_models:
                continue
            try:
                kwargs = self._build_llm_kwargs(
                    model, messages, tools, input_est, temperature, stream=True
                )
                stream_start = time.monotonic()
                stream = await litellm.acompletion(**kwargs)

                chunks: list[Any] = []
                accumulated_content = ""
                has_tool_calls = False
                ttft_logged = False

                async for chunk in stream:
                    chunks.append(chunk)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if getattr(delta, "content", None):
                        if not ttft_logged:
                            ttft_ms = int((time.monotonic() - stream_start) * 1000)
                            logger.info("TTFT %dms model=%s", ttft_ms, model)
                            ttft_logged = True
                        accumulated_content += delta.content
                        if not has_tool_calls:
                            with contextlib.suppress(Exception):
                                await on_content(accumulated_content)
                    if getattr(delta, "tool_calls", None):
                        has_tool_calls = True

                return litellm.stream_chunk_builder(chunks)
            except Exception as e:
                self._handle_model_error(e, model, broken_models, streaming=True)
                last_error = e

        logger.error("All models failed (streaming). Last error: %s", last_error)
        return None

    def _finish_run(self, run: AgentRun, trace: Any = None) -> AgentRun:
        """Persist the final run state to the database."""
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
            )
            # Record steps
            for step in run.steps:
                try:
                    create_step(step)
                except Exception as e:
                    logger.warning("Failed to record step: %s", e)
        except Exception as e:
            logger.warning("Failed to update run in database: %s", e)

        # Auto-resolve CRM task linked to this run
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
                logger.warning("Auto-task update failed: %s", e)

        return run
