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
from collections.abc import Awaitable, Callable
from typing import Any

import litellm

from robothor.engine.config import (
    EngineConfig,
    build_system_prompt,
    load_agent_config,
)
from robothor.engine.models import AgentConfig, AgentRun, SpawnContext, TriggerType
from robothor.engine.session import AgentSession
from robothor.engine.tools import get_registry
from robothor.engine.tracking import create_run, create_step, update_run

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True

# Register custom pricing for OpenRouter-routed models.
# Without this, litellm.completion_cost() returns $0.00 for these models.
litellm.register_model(
    {
        "openrouter/moonshotai/kimi-k2.5": {
            "max_tokens": 131072,
            "input_cost_per_token": 0.0000006,  # $0.60/M
            "output_cost_per_token": 0.0000024,  # $2.40/M
        },
        "openrouter/anthropic/claude-sonnet-4.6": {
            "max_tokens": 8192,
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
        on_tool: Callable[[dict], Awaitable[None]] | None = None,
        model_override: str | None = None,
        conversation_history: list[dict] | None = None,
        resume_from_run_id: str | None = None,
        spawn_context: SpawnContext | None = None,
    ) -> AgentRun:
        """Execute an agent with the given message.

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

        # Build system prompt
        system_prompt = build_system_prompt(agent_config, self.config.workspace)

        # Get filtered tools for this agent
        tool_schemas = self.registry.build_for_agent(agent_config)
        tool_names = self.registry.get_tool_names(agent_config)

        # Start session
        session.start(
            system_prompt=system_prompt,
            user_message=message,
            tools_provided=tool_names,
            delivery_mode=agent_config.delivery_mode.value,
            conversation_history=conversation_history,
        )

        # Copy budget config to run for persistence
        session.run.token_budget = agent_config.token_budget
        session.run.cost_budget_usd = agent_config.cost_budget_usd

        # Sub-agent: cascade parent's remaining budget (child can never exceed parent)
        if spawn_context:
            if spawn_context.remaining_token_budget > 0:
                if agent_config.token_budget > 0:
                    session.run.token_budget = min(
                        agent_config.token_budget, spawn_context.remaining_token_budget
                    )
                else:
                    session.run.token_budget = spawn_context.remaining_token_budget
                agent_config.token_budget = session.run.token_budget

            if spawn_context.remaining_cost_budget_usd > 0:
                if agent_config.cost_budget_usd > 0:
                    session.run.cost_budget_usd = min(
                        agent_config.cost_budget_usd, spawn_context.remaining_cost_budget_usd
                    )
                else:
                    session.run.cost_budget_usd = spawn_context.remaining_cost_budget_usd
                agent_config.cost_budget_usd = session.run.cost_budget_usd

        # Record run in database
        try:
            create_run(session.run)
        except Exception as e:
            logger.warning("Failed to record run start: %s", e)

        # Build model list for fallback (model_override takes priority)
        if model_override:
            models = [model_override, agent_config.model_primary] + agent_config.model_fallbacks
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

        # ── [CHECKPOINT] Resume from checkpoint if requested ──
        resumed_scratchpad = None
        if resume_from_run_id:
            resumed_scratchpad = self._resume_from_checkpoint(resume_from_run_id, session)

        # Execute with timeout
        timeout = agent_config.timeout_seconds
        try:
            async with asyncio.timeout(timeout):
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
        if self._should_verify(agent_config, route):
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

    async def _run_loop(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict],
        agent_config: AgentConfig,
        on_content: Callable[[str], Awaitable[None]] | None = None,
        on_tool: Callable[[dict], Awaitable[None]] | None = None,
        *,
        max_iterations: int = 20,
        route: Any = None,
        plan_result: Any = None,
        trace: Any = None,
        resumed_scratchpad: Any = None,
        spawn_context: SpawnContext | None = None,
    ) -> None:
        """Core conversation loop: LLM call → tool execution → repeat."""
        # Track models that hit permanent errors (401/403/429) across iterations
        broken_models: set[str] = set()

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
                remaining_token_budget=agent_config.token_budget,
                remaining_cost_budget_usd=agent_config.cost_budget_usd,
                parent_trace_id=trace.trace_id if trace else "",
                parent_span_id="",
            )
            _current_spawn_context.set(fresh_ctx)

        # Compress context for persistent sessions
        if agent_config.session_target == "persistent":
            try:
                from robothor.engine.context import maybe_compress

                session.messages = await maybe_compress(session.messages, models)
            except Exception as e:
                logger.debug("Context compression failed: %s", e)

        # ── v2: Initialize enhancement objects ──
        scratchpad = self._create_scratchpad(agent_config, route, resumed_scratchpad)
        escalation = self._create_escalation(agent_config)
        checkpoint = self._create_checkpoint(agent_config, route, session.run_id)
        guardrail_engine = self._create_guardrails(agent_config)

        budget_warning_sent = False
        plan_steps = 0
        if plan_result and hasattr(plan_result, "estimated_steps"):
            plan_steps = plan_result.estimated_steps

        for _iteration in range(max_iterations):
            # ── [BUDGET] Check token/cost budget ──
            budget_status = session.check_budget(
                agent_config.token_budget, agent_config.cost_budget_usd
            )
            if budget_status == "exhausted":
                session.run.budget_exhausted = True
                session.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[SYSTEM] Budget exhausted. You must wrap up immediately. "
                            "Summarize your progress and any remaining work."
                        ),
                    }
                )
                # Allow one more LLM call to wrap up, then break
                await self._llm_call_and_record(
                    session,
                    models,
                    tool_schemas,
                    on_content,
                    broken_models,
                    agent_config.temperature,
                    trace,
                )
                return
            if budget_status == "warning" and not budget_warning_sent:
                budget_warning_sent = True
                session.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[SYSTEM] You are approaching your budget limit (>80%). "
                            "Prioritize the most important remaining work."
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
                return

            # ── Execute tool calls ──
            iteration_errors: list[tuple[str, str]] = []

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
                        iteration_errors.append((tool_name, gr_error_msg))
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

                # ── [SCRATCHPAD] Record tool call ──
                if scratchpad:
                    scratchpad.record_tool_call(tool_name, error=error_msg)

                # ── [ESCALATION] Record error/success ──
                if escalation:
                    if error_msg:
                        escalation.record_error()
                    else:
                        escalation.record_success()

                # ── [CHECKPOINT] Record success ──
                if checkpoint and not error_msg:
                    checkpoint.record_success()

                # Track errors for this iteration
                if error_msg:
                    iteration_errors.append((tool_name, error_msg))

            # ── [ERROR FEEDBACK] Inject analysis prompt on errors ──
            if iteration_errors and agent_config.error_feedback:
                error_lines = "\n".join(f"- {name}: {msg}" for name, msg in iteration_errors)
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
                    session.record_error(f"Hard abort: {escalation.total_errors} total errors")
                    return
                esc_msg = escalation.get_escalation_message()
                if esc_msg:
                    session.messages.append({"role": "user", "content": esc_msg})

            # ── [CHECKPOINT] Save state ──
            if checkpoint and checkpoint.should_checkpoint():
                checkpoint.save(
                    step_number=session._step_counter,
                    messages=session.messages,
                    scratchpad=scratchpad.to_dict() if scratchpad else None,
                    plan=plan_result.raw if plan_result and hasattr(plan_result, "raw") else None,
                )

        # Hit max iterations
        session.record_error(f"Max iterations reached ({max_iterations})")

    # ─── LLM call helper (shared by main loop and budget wrap-up) ─────

    async def _llm_call_and_record(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict],
        on_content: Callable[[str], Awaitable[None]] | None,
        broken_models: set[str],
        temperature: float,
        trace: Any = None,
    ) -> tuple[Any, str, int, dict]:
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

        # Build assistant message dict
        msg_dict: dict[str, Any] = {"role": "assistant"}
        if assistant_msg.content:
            msg_dict["content"] = assistant_msg.content
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
            cost = litellm.completion_cost(completion_response=response)
            if cost:
                session.run.total_cost_usd += cost
        except Exception:
            pass

        return response, model_used, elapsed_ms, msg_dict

    async def _do_llm_call(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict],
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
        """Create GuardrailEngine if policies are configured."""
        if not agent_config.guardrails:
            return None
        try:
            from robothor.engine.guardrails import GuardrailEngine

            return GuardrailEngine(enabled_policies=agent_config.guardrails)
        except Exception:
            return None

    def _should_verify(self, agent_config: AgentConfig, route: Any) -> bool:
        """Determine if verification step should run."""
        if agent_config.verification_enabled:
            return True
        return bool(route and route.verification is True)

    async def _run_verification(
        self,
        agent_config: AgentConfig,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict],
        output_text: str | None,
        on_content,
        on_tool,
        **loop_kwargs,
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
                fallback_models=models[1:2],
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

    # ─── LLM Call Methods (unchanged) ─────────────────────────────────

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        """Call LLM with model fallback. Returns litellm response or None."""
        last_error = None

        for model in models:
            if broken_models and model in broken_models:
                continue

            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": 8192,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                response = await litellm.acompletion(**kwargs)
                return response

            except Exception as e:
                status = getattr(e, "status_code", None)
                if broken_models is not None and status in (401, 403, 429):
                    broken_models.add(model)
                    logger.warning(
                        "Model %s permanently failed (%s), removing from rotation",
                        model,
                        status,
                    )
                else:
                    logger.warning("Model %s failed: %s", model, e)
                last_error = e
                continue

        logger.error("All models failed. Last error: %s", last_error)
        return None

    async def _call_llm_streaming(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict],
        on_content: Callable[[str], Awaitable[None]],
        broken_models: set[str] | None = None,
        temperature: float = 0.3,
    ) -> Any:
        """Call LLM with streaming. Streams text content via on_content callback.

        Returns a reconstructed ModelResponse identical to non-streaming _call_llm,
        so the rest of the loop processes it the same way.
        """
        last_error = None

        for model in models:
            if broken_models and model in broken_models:
                continue

            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": 8192,
                    "stream": True,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                stream = await litellm.acompletion(**kwargs)

                chunks: list = []
                accumulated_content = ""
                has_tool_calls = False

                async for chunk in stream:
                    chunks.append(chunk)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # Accumulate text content
                    if getattr(delta, "content", None):
                        accumulated_content += delta.content
                        # Stream to callback only if no tool calls detected yet
                        if not has_tool_calls:
                            with contextlib.suppress(Exception):
                                await on_content(accumulated_content)
                    # Track tool call presence
                    if getattr(delta, "tool_calls", None):
                        has_tool_calls = True

                # Reconstruct full response from chunks
                response = litellm.stream_chunk_builder(chunks)
                return response

            except Exception as e:
                status = getattr(e, "status_code", None)
                if broken_models is not None and status in (401, 403, 429):
                    broken_models.add(model)
                    logger.warning(
                        "Model %s permanently failed (%s), removing from rotation",
                        model,
                        status,
                    )
                else:
                    logger.warning("Model %s (streaming) failed: %s", model, e)
                last_error = e
                continue

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
                token_budget=run.token_budget if run.token_budget else None,
                cost_budget_usd=run.cost_budget_usd if run.cost_budget_usd else None,
                budget_exhausted=run.budget_exhausted if run.budget_exhausted else None,
            )
            # Record steps
            for step in run.steps:
                try:
                    create_step(step)
                except Exception as e:
                    logger.warning("Failed to record step: %s", e)
        except Exception as e:
            logger.warning("Failed to update run in database: %s", e)

        return run
