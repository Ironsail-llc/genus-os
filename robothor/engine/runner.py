"""
Agent Runner — core LLM conversation loop with tool calling.

Uses litellm for unified LLM API with model fallback.
Executes tools directly via the ToolRegistry (DAL calls, no HTTP).

Usage:
    runner = AgentRunner(engine_config)
    run = await runner.execute("email-classifier", "Process triage inbox")
"""

from __future__ import annotations

import asyncio
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
from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType
from robothor.engine.session import AgentSession
from robothor.engine.tools import get_registry
from robothor.engine.tracking import create_run, create_step, update_run

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True


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
        model_override: str | None = None,
        conversation_history: list[dict] | None = None,
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

        # Execute with timeout
        timeout = agent_config.timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                await self._run_loop(session, models, tool_schemas, agent_config, on_content)
        except asyncio.TimeoutError:
            logger.warning("Agent %s timed out after %ds", agent_id, timeout)
            session.record_error(f"Timed out after {timeout}s")
            return self._finish_run(session.timeout())
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("Agent %s failed: %s", agent_id, e, exc_info=True)
            session.record_error(str(e), tb)
            return self._finish_run(session.fail(str(e), tb))

        # Extract final output
        output_text = session.get_final_text()
        return self._finish_run(session.complete(output_text))

    async def _run_loop(
        self,
        session: AgentSession,
        models: list[str],
        tool_schemas: list[dict],
        agent_config: AgentConfig,
        on_content: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Core conversation loop: LLM call → tool execution → repeat."""
        max_iterations = self.config.max_iterations

        for iteration in range(max_iterations):
            # Call LLM (streaming when callback provided)
            start = time.monotonic()
            if on_content:
                response = await self._call_llm_streaming(
                    session.messages, models, tool_schemas, on_content
                )
            else:
                response = await self._call_llm(session.messages, models, tool_schemas)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if response is None:
                session.record_error("All models failed")
                raise RuntimeError("All models failed to respond")

            if not response.choices:
                session.record_error("LLM returned empty choices")
                raise RuntimeError("LLM returned empty choices")

            # Extract response data
            choice = response.choices[0]
            assistant_msg = choice.message
            model_used = response.model or models[0]

            # Build assistant message dict for conversation history
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
            session.record_llm_call(
                model=model_used,
                input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                duration_ms=elapsed_ms,
                assistant_message=msg_dict,
            )

            # Check if we're done (no tool calls)
            if not assistant_msg.tool_calls:
                return

            # Execute tool calls
            for tc in assistant_msg.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                tool_start = time.monotonic()
                result = await self.registry.execute(
                    tool_name,
                    tool_args,
                    agent_id=agent_config.id,
                    tenant_id=session.run.tenant_id,
                    workspace=str(self.config.workspace),
                )
                tool_elapsed = int((time.monotonic() - tool_start) * 1000)

                error_msg = result.get("error") if isinstance(result, dict) else None
                session.record_tool_call(
                    tool_name=tool_name,
                    tool_input=tool_args,
                    tool_output=result,
                    tool_call_id=tc.id,
                    duration_ms=tool_elapsed,
                    error_message=error_msg,
                )

        # Hit max iterations
        session.record_error(f"Max iterations reached ({max_iterations})")

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict],
    ) -> Any:
        """Call LLM with model fallback. Returns litellm response or None."""
        last_error = None

        for model in models:
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 8192,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                response = await litellm.acompletion(**kwargs)
                return response

            except Exception as e:
                last_error = e
                logger.warning("Model %s failed: %s", model, e)
                continue

        logger.error("All models failed. Last error: %s", last_error)
        return None

    async def _call_llm_streaming(
        self,
        messages: list[dict[str, Any]],
        models: list[str],
        tools: list[dict],
        on_content: Callable[[str], Awaitable[None]],
    ) -> Any:
        """Call LLM with streaming. Streams text content via on_content callback.

        Returns a reconstructed ModelResponse identical to non-streaming _call_llm,
        so the rest of the loop processes it the same way.
        """
        last_error = None

        for model in models:
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
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
                            try:
                                await on_content(accumulated_content)
                            except Exception:
                                pass
                    # Track tool call presence
                    if getattr(delta, "tool_calls", None):
                        has_tool_calls = True

                # Reconstruct full response from chunks
                response = litellm.stream_chunk_builder(chunks)
                return response

            except Exception as e:
                last_error = e
                logger.warning("Model %s (streaming) failed: %s", model, e)
                continue

        logger.error("All models failed (streaming). Last error: %s", last_error)
        return None

    def _finish_run(self, run: AgentRun) -> AgentRun:
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
