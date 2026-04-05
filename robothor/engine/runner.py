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

# ── Log-injection sanitizer ──
# CodeQL py/log-injection: user-controlled values (model names, error
# messages) must not inject newlines into log output.
from robothor.engine.sanitize import sanitize_log as _sanitize  # noqa: E402
from robothor.engine.session import ENGINE_CONTEXT_ROLE, AgentSession
from robothor.engine.tools import get_registry
from robothor.engine.tracking import create_run, create_step, update_run

# Max seconds to wait for the next streaming chunk before aborting and
# falling back to the next model.  Prevents stalled streams from hanging
# the entire run (the stream *creation* timeout is separate — 120 s).
STREAM_CHUNK_TIMEOUT = 60


class _StallWatchdog:
    """Kills a run if no activity occurs for stall_timeout seconds.

    Activity is tracked via touch() — call it on every LLM response,
    tool completion, and sub-agent completion. A background task checks
    every 30s and cancels the given asyncio.Task if idle too long.

    If stall_timeout <= 0, the watchdog is disabled (no-op).
    Hard timeout_seconds is kept as an absolute safety net.
    """

    def __init__(self, stall_timeout: int, hard_timeout: int) -> None:
        self._stall_timeout = stall_timeout
        self._hard_timeout = hard_timeout
        self._last_activity = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self._cancelled = False

    def touch(self) -> None:
        """Record activity — resets the stall timer."""
        self._last_activity = time.monotonic()

    def start(self, monitored_task: asyncio.Task[Any]) -> None:
        """Start the watchdog background loop."""
        if self._stall_timeout <= 0 and self._hard_timeout <= 0:
            return
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._watch(monitored_task))

    async def _watch(self, monitored_task: asyncio.Task[Any]) -> None:
        try:
            while not monitored_task.done():
                await asyncio.sleep(30)
                if monitored_task.done():
                    break
                now = time.monotonic()
                idle = now - self._last_activity
                elapsed = now - self._start_time

                # Hard timeout (absolute safety net)
                if self._hard_timeout > 0 and elapsed > self._hard_timeout:
                    logger.warning(
                        "Stall watchdog: hard timeout (%ds) reached after %.0fs",
                        self._hard_timeout,
                        elapsed,
                    )
                    self._cancelled = True
                    monitored_task.cancel()
                    return

                # Stall detection (primary mechanism)
                if self._stall_timeout > 0 and idle > self._stall_timeout:
                    logger.warning(
                        "Stall watchdog: no activity for %.0fs (limit %ds), killing run",
                        idle,
                        self._stall_timeout,
                    )
                    self._cancelled = True
                    monitored_task.cancel()
                    return
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """Stop the watchdog."""
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def was_stall_timeout(self) -> bool:
        return self._cancelled


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
        "openrouter/xiaomi/mimo-v2-pro": {
            "max_tokens": 1000000,
            "input_cost_per_token": 0.000001,  # $0.80/M
            "output_cost_per_token": 0.000003,  # $2.56/M
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
        self._active_watchdog: _StallWatchdog | None = None

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
            logger.error("Agent config not found: %s", _sanitize(agent_id))
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
            tool_offload_threshold=agent_config.tool_offload_threshold,
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
        system_prompt_parts = await sys_prompt_future  # SystemPromptParts
        system_prompt = system_prompt_parts.full_text()  # str for mode wrapping
        warmup_preamble: str | None = None
        if warmup_future is not None:
            try:
                warmup_preamble = await warmup_future
            except Exception as e:
                logger.debug("Warmup preamble failed for %s: %s", _sanitize(agent_id), _sanitize(e))

        if warmup_preamble:
            message = f"{warmup_preamble}\n\n{message}"

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

        # Stall watchdog is the primary protection — kills on inactivity, not
        # elapsed wall-clock time.  Hard timeout only needed as fallback when
        # the watchdog is explicitly disabled (stall_timeout_seconds: 0).
        # This lets agents run for hours on complex tasks without being killed.
        stall_timeout = getattr(agent_config, "stall_timeout_seconds", 300)
        hard_timeout = agent_config.timeout_seconds if agent_config.timeout_seconds > 0 else None
        watchdog = _StallWatchdog(
            stall_timeout=stall_timeout, hard_timeout=agent_config.timeout_seconds
        )
        self._active_watchdog = watchdog  # expose for touch() calls from tool handlers
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

                # ── [SANDBOX] Create sandbox for computer-use agents ──
                sandbox = None
                if agent_config.sandbox == "docker":
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

                # Start stall watchdog — monitors current task for inactivity
                current_task = asyncio.current_task()
                if current_task:
                    watchdog.start(current_task)

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
                    watchdog.stop()
                    # Cleanup sandbox if created
                    if sandbox:
                        try:
                            await sandbox.stop()
                        except Exception as e:
                            logger.warning("Sandbox cleanup failed: %s", _sanitize(e))
                        from robothor.engine.sandbox import set_current_sandbox

                        set_current_sandbox(None)

        except (TimeoutError, asyncio.CancelledError):
            if watchdog.was_stall_timeout:
                idle_msg = f"Stall watchdog: no activity for {stall_timeout}s"
                logger.warning("Agent %s killed: %s", _sanitize(agent_id), idle_msg)
                session.record_error(idle_msg)
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
                return self._finish_run(session.timeout(), trace=trace)
            # Hard timeout (only fires when stall watchdog is disabled)
            ht = agent_config.timeout_seconds
            logger.warning("Agent %s hard-timed out after %ds", _sanitize(agent_id), ht)
            session.record_error(f"Hard timeout after {ht}s")
            return self._finish_run(session.timeout(), trace=trace)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("Agent %s failed: %s", _sanitize(agent_id), _sanitize(e), exc_info=True)
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
                on_stream_event=on_stream_event,
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
                        "cache_creation_tokens": session.run.cache_creation_tokens,
                        "cache_read_tokens": session.run.cache_read_tokens,
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

        # Build readonly tool set for runtime enforcement in plan mode
        _readonly_tool_set: frozenset[str] = frozenset()
        if readonly_mode:
            from robothor.engine.tools.constants import READONLY_TOOLS

            _readonly_tool_set = READONLY_TOOLS

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
        _pre_iteration_msg_idx = len(session.messages)
        _tool_failures: dict[str, int] = {}  # per-tool failure count for circuit breaker

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

            # ── [BUDGET] Token and cost tracking with enforcement ──
            budget_status = session.check_budget(
                session.run.token_budget, agent_config.max_cost_usd
            )
            if budget_status == "exhausted" and not session.run.budget_exhausted:
                session.run.budget_exhausted = True  # track for analytics
                if agent_config.hard_budget:
                    # Hard budget: force wrap-up and exit
                    await self._force_wrapup(
                        session,
                        models,
                        tool_schemas,
                        on_content,
                        broken_models,
                        agent_config.temperature,
                        trace,
                        reason="Hard budget limit reached (token or cost cap).",
                    )
                    return
                # Soft enforcement: tell the agent to wrap up
                session.messages.append(
                    {
                        "role": ENGINE_CONTEXT_ROLE,
                        "content": (
                            "[SYSTEM] Budget limit reached. Wrap up your current task "
                            "in this final response. Do not start new tool calls."
                        ),
                    }
                )
                budget_exhausted_iters = 0
            if budget_status == "warning" and not budget_warning_sent:
                budget_warning_sent = True
                session.messages.append(
                    {
                        "role": ENGINE_CONTEXT_ROLE,
                        "content": (
                            "[SYSTEM] Token usage note: you have used >80% of the "
                            "estimated token budget for this run. Begin wrapping up "
                            "and provide your final response soon."
                        ),
                    }
                )
                # Dispatch BUDGET_WARNING hook
                if hook_registry:
                    with contextlib.suppress(Exception):
                        await hook_registry.dispatch(
                            HookEvent.BUDGET_WARNING,
                            HookContext(
                                event=HookEvent.BUDGET_WARNING,
                                agent_id=agent_config.id,
                                run_id=session.run_id,
                                metadata={
                                    "token_budget": session.run.token_budget,
                                    "tokens_used": session.run.input_tokens
                                    + session.run.output_tokens,
                                    "cost_usd": session.run.total_cost_usd,
                                },
                            ),
                        )

            # Soft budget enforcement: after 2 iterations past exhaustion,
            # strip tool schemas to force a text-only response
            if session.run.budget_exhausted:
                budget_exhausted_iters = getattr(session.run, "_budget_exhausted_iters", 0) + 1
                session.run._budget_exhausted_iters = budget_exhausted_iters  # type: ignore[attr-defined]
                if budget_exhausted_iters > 2:
                    tool_schemas = []  # force text-only response

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
                    model_limits = get_model_limits(models[0])
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
                        tool_name, tool_args, agent_id=agent_config.id
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
                            else:
                                pass  # no manager = auto-approve (autonomous default)
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
                _tool_timeout = getattr(agent_config, "tool_timeout_seconds", 120)
                if trace:
                    with trace.span("tool_call", tool=tool_name) as _span:
                        result = await self.registry.execute(
                            tool_name,
                            tool_args,
                            agent_id=agent_config.id,
                            tenant_id=session.run.tenant_id,
                            workspace=str(self.config.workspace),
                            timeout=_tool_timeout,
                        )
                else:
                    result = await self.registry.execute(
                        tool_name,
                        tool_args,
                        agent_id=agent_config.id,
                        tenant_id=session.run.tenant_id,
                        workspace=str(self.config.workspace),
                        timeout=_tool_timeout,
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
                    self._active_watchdog.touch()

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
                checkpoint.save(
                    step_number=session._step_counter,
                    messages=session.messages,
                    scratchpad=scratchpad.to_dict() if scratchpad else None,
                    plan=plan_result.raw if plan_result and hasattr(plan_result, "raw") else None,
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
        """
        session.record_error(reason)
        session.messages.append(
            {
                "role": ENGINE_CONTEXT_ROLE,
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
            self._active_watchdog.touch()

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
        try:
            cost = litellm.completion_cost(completion_response=response, model=models[0])
            if cost and cost > 0:
                session.run.total_cost_usd += cost
            else:
                cost = self._calculate_cost(
                    models[0],
                    input_tokens,
                    output_tokens,
                    cache_creation_tokens,
                    cache_read_tokens,
                )
                session.run.total_cost_usd += cost
        except Exception as e:
            logger.warning("litellm cost calculation failed, using fallback: %s", e)
            cost = self._calculate_cost(
                models[0],
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
            )
            session.run.total_cost_usd += cost

        # Record step cost for projection (used by hard budget pre-flight check)
        session.record_step_cost(cost or 0.0)

        return response, model_used, elapsed_ms, msg_dict

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Calculate cost from litellm model registry, with cache-aware pricing."""
        info = litellm.model_cost.get(model, {})
        input_rate: float = info.get("input_cost_per_token", 0.0)
        output_rate: float = info.get("output_cost_per_token", 0.0)

        # Use our registry for cache pricing (litellm doesn't expose these yet)
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits(model)
        cache_write_rate = limits.cache_write_cost_per_token or input_rate
        cache_read_rate = limits.cache_read_cost_per_token or (input_rate * 0.1)

        # Coerce to int to guard against MagicMock or None from test mocks
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        cache_read_tokens = int(cache_read_tokens or 0)

        # Non-cached input = total input minus tokens that were cache hits
        regular_input = max(0, input_tokens - cache_read_tokens)
        return (
            regular_input * input_rate
            + output_tokens * output_rate
            + cache_creation_tokens * cache_write_rate
            + cache_read_tokens * cache_read_rate
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
        """Dispatch to streaming or non-streaming LLM call."""
        if on_content or on_stream_event:
            return await self._call_llm_streaming(
                session.messages,
                models,
                tool_schemas,
                on_content,
                broken_models=broken_models,
                temperature=temperature,
                on_stream_event=on_stream_event,
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
                max_spawn_batch=ctx.max_spawn_batch,
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
            logger.warning("Pre-flight compression failed: %s", _sanitize(e))

        return estimate_tokens(messages)

    @staticmethod
    def _validate_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop orphaned tool messages whose tool_call_id has no matching tool_use.

        This is a defense-in-depth measure: if compaction, checkpoint restore,
        or format conversion ever corrupts the message history, this prevents
        cryptic "unexpected tool_use_id in tool_result blocks" API errors.
        """
        valid_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        valid_ids.add(tc_id)

        if not valid_ids:
            return messages  # no tool calls at all — nothing to validate

        cleaned: list[dict[str, Any]] = []
        dropped = 0
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") not in valid_ids:
                dropped += 1
                continue
            cleaned.append(msg)

        if dropped:
            logger.warning(
                "Dropped %d orphaned tool_result message(s) — "
                "no matching tool_use in assistant messages",
                dropped,
            )

        return cleaned if dropped else messages

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

        # For direct Anthropic API models, enable prompt caching on the system
        # message by converting it to content-block format with cache_control.
        # OpenRouter models (e.g. "openrouter/anthropic/claude-sonnet-4-6") must
        # NOT get this conversion — litellm sends them via the OpenAI-compatible
        # path and the mixed format causes tool_use/tool_result pairing failures.
        is_direct_anthropic = model.startswith("anthropic/")
        if is_direct_anthropic and messages and messages[0].get("role") == "system":
            messages = list(messages)  # shallow copy to avoid mutating original
            sys_content = messages[0].get("content")
            if isinstance(sys_content, str):
                sys_msg = dict(messages[0])
                # Split into static (cacheable) + dynamic (time context) blocks.
                # The dynamic tail starts at the last "---" separator before "Current time:".
                split_marker = "\n\n---\n\nCurrent time:"
                split_idx = sys_content.rfind(split_marker)
                if split_idx > 0:
                    static_part = sys_content[:split_idx]
                    dynamic_part = sys_content[split_idx + len("\n\n---\n\n") :]
                    sys_msg["content"] = [
                        {
                            "type": "text",
                            "text": static_part,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": dynamic_part,
                        },
                    ]
                else:
                    # Fallback: cache the whole thing (no time context found)
                    sys_msg["content"] = [
                        {
                            "type": "text",
                            "text": sys_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                messages[0] = sys_msg

        # Defense in depth: drop orphaned tool_result messages that would
        # cause "unexpected tool_use_id" API errors.
        messages = AgentRunner._validate_tool_pairs(messages)

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
        is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError))
        # Mark broken for auth, rate limit, provider failures, and timeouts
        if broken_models is not None and (
            status in (401, 402, 403, 429, 500, 502, 503, 504) or is_timeout
        ):
            # First model to fail = primary model — log at ERROR for visibility
            is_primary = len(broken_models) == 0
            broken_models.add(model)
            reason = "timeout" if is_timeout else str(status)
            if is_primary:
                logger.error(
                    "PRIMARY model %s failed (%s), falling back — primary_model_fallback=True",
                    _sanitize(model),
                    _sanitize(reason),
                )
            else:
                logger.warning(
                    "Model %s failed (%s), removing from rotation for this run",
                    _sanitize(model),
                    _sanitize(reason),
                )
        else:
            suffix = " (streaming)" if streaming else ""
            logger.warning("Model %s%s failed: %s", _sanitize(model), suffix, _sanitize(e))

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
        last_error: Exception | None = None

        # Divide total timeout budget across fallback models so worst-case
        # wall-clock time stays bounded (instead of N * 120s).
        total_timeout_budget = 180  # total time for all fallbacks
        per_model_timeout = max(30, total_timeout_budget // len(models))

        logger.debug(
            "LLM call with models: %s (broken: %s)",
            _sanitize(models),
            _sanitize(broken_models or set()),
        )
        for model in models:
            if broken_models and model in broken_models:
                continue
            try:
                kwargs = self._build_llm_kwargs(model, messages, tools, input_est, temperature)
                return await asyncio.wait_for(
                    litellm.acompletion(**kwargs), timeout=per_model_timeout
                )
            except TimeoutError:
                self._handle_model_error(
                    TimeoutError(f"LLM call to {model} hung for {per_model_timeout}s"),
                    model,
                    broken_models,
                )
                last_error = TimeoutError(f"Model {model} timed out after {per_model_timeout}s")
                # Model rotation is activity — don't let watchdog kill us mid-fallback
                if self._active_watchdog:
                    self._active_watchdog.touch()
            except Exception as e:
                self._handle_model_error(e, model, broken_models)
                last_error = e
                if self._active_watchdog:
                    self._active_watchdog.touch()

        logger.error(
            "All models failed. Models: %s, broken: %s, last error: %s",
            _sanitize(models),
            _sanitize(broken_models or set()),
            _sanitize(last_error),
        )
        return None

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
        """Call LLM with streaming. Returns reconstructed ModelResponse.

        Emits structured events to ``on_stream_event`` if provided:
        - ``{"type": "text_delta", "delta": "...", "accumulated": "..."}``
        - ``{"type": "tool_use_start", "tool_name": "...", "call_id": "..."}``
        - ``{"type": "tool_use_delta", "delta": "...", "call_id": "..."}``
        - ``{"type": "usage", "input_tokens": N, "output_tokens": N}``
        - ``{"type": "message_stop"}``
        """
        input_est = await self._prepare_llm_call(messages, models)
        last_error: Exception | None = None

        # Divide total timeout budget across fallback models so worst-case
        # wall-clock time stays bounded (instead of N * 120s).
        total_timeout_budget = 180  # total time for all fallbacks
        per_model_timeout = max(30, total_timeout_budget // len(models))

        async def _emit(event: dict[str, Any]) -> None:
            if on_stream_event:
                with contextlib.suppress(Exception):
                    await on_stream_event(event)

        for model in models:
            if broken_models and model in broken_models:
                continue
            try:
                kwargs = self._build_llm_kwargs(
                    model, messages, tools, input_est, temperature, stream=True
                )
                stream_start = time.monotonic()
                stream = await asyncio.wait_for(
                    litellm.acompletion(**kwargs), timeout=per_model_timeout
                )

                chunks: list[Any] = []
                accumulated_content = ""
                has_tool_calls = False
                ttft_logged = False
                seen_tool_ids: set[str] = set()

                # Consume stream with per-chunk timeout so stalled streams
                # fall back to the next model instead of hanging the run.
                chunk_iter = stream.__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            chunk_iter.__anext__(), timeout=STREAM_CHUNK_TIMEOUT
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        logger.warning(
                            "Stream stalled for %ds, aborting model=%s",
                            STREAM_CHUNK_TIMEOUT,
                            _sanitize(model),
                        )
                        raise TimeoutError(
                            f"Stream stalled after {STREAM_CHUNK_TIMEOUT}s of no chunks"
                        ) from None

                    chunks.append(chunk)

                    # Touch watchdog on each chunk so active streams stay alive
                    if self._active_watchdog:
                        self._active_watchdog.touch()

                    if not chunk.choices:
                        # Check for usage in non-choice chunks (some providers)
                        usage = getattr(chunk, "usage", None)
                        if usage:
                            await _emit(
                                {
                                    "type": "usage",
                                    "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                                    "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                                }
                            )
                        continue
                    delta = chunk.choices[0].delta
                    if getattr(delta, "content", None):
                        if not ttft_logged:
                            ttft_ms = int((time.monotonic() - stream_start) * 1000)
                            logger.info("TTFT %dms model=%s", ttft_ms, _sanitize(model))
                            ttft_logged = True
                        accumulated_content += delta.content
                        await _emit(
                            {
                                "type": "text_delta",
                                "delta": delta.content,
                                "accumulated": accumulated_content,
                            }
                        )
                        if not has_tool_calls and on_content:
                            with contextlib.suppress(Exception):
                                await on_content(accumulated_content)
                    if getattr(delta, "tool_calls", None):
                        has_tool_calls = True
                        for tc in delta.tool_calls:
                            tc_id = getattr(tc, "id", None)
                            tc_fn = getattr(tc, "function", None)
                            if tc_id and tc_id not in seen_tool_ids:
                                seen_tool_ids.add(tc_id)
                                await _emit(
                                    {
                                        "type": "tool_use_start",
                                        "tool_name": getattr(tc_fn, "name", "") if tc_fn else "",
                                        "call_id": tc_id,
                                    }
                                )
                            if tc_fn and getattr(tc_fn, "arguments", None):
                                await _emit(
                                    {
                                        "type": "tool_use_delta",
                                        "delta": tc_fn.arguments,
                                        "call_id": tc_id or "",
                                    }
                                )

                await _emit({"type": "message_stop"})
                return litellm.stream_chunk_builder(chunks)
            except TimeoutError as te:
                self._handle_model_error(
                    te,
                    model,
                    broken_models,
                    streaming=True,
                )
                last_error = te
                # Model rotation is activity — don't let watchdog kill us mid-fallback
                if self._active_watchdog:
                    self._active_watchdog.touch()
            except Exception as e:
                self._handle_model_error(e, model, broken_models, streaming=True)
                last_error = e
                if self._active_watchdog:
                    self._active_watchdog.touch()

        logger.error("All models failed (streaming). Last error: %s", last_error)
        return None

    def _finish_run(self, run: AgentRun, trace: Any = None) -> AgentRun:
        """Persist the final run state to the database."""
        # ── [HOOKS] AGENT_END lifecycle hook (fire-and-forget) ──
        try:
            from robothor.engine.hook_registry import HookContext, HookEvent, get_hook_registry

            hr = get_hook_registry()
            if hr:
                import asyncio

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
                # Dispatch as fire-and-forget if we're in an event loop
                try:
                    asyncio.get_running_loop()  # verify loop exists
                    from robothor.engine.task_registry import get_task_registry

                    get_task_registry().spawn(
                        hr.dispatch(HookEvent.AGENT_END, end_ctx),
                        name=f"agent-end-hook:{run.agent_id}",
                    )
                except RuntimeError:
                    pass  # No event loop — skip async dispatch
        except Exception as e:
            logger.warning("AGENT_END hook error: %s", _sanitize(e))

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
            )
            # Record steps
            for step in run.steps:
                try:
                    create_step(step)
                except Exception as e:
                    logger.warning("Failed to record step: %s", _sanitize(e))
        except Exception as e:
            logger.warning("Failed to update run in database: %s", _sanitize(e))

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
                logger.warning("Auto-task update failed: %s", _sanitize(e))

        return run
