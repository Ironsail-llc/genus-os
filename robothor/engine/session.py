"""
Agent Session — per-run message state and step recording.

Manages the conversation history for a single agent run:
- Tracks system prompt, user message, and all LLM/tool exchanges
- Records each step to the tracking DAL
- Accumulates token counts and cost
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from robothor.engine.models import AgentRun, RunStatus, RunStep, StepType, TriggerType

logger = logging.getLogger(__name__)


class AgentSession:
    """Per-run state manager for an agent execution."""

    def __init__(
        self,
        agent_id: str,
        trigger_type: TriggerType = TriggerType.MANUAL,
        trigger_detail: str | None = None,
        tenant_id: str = "robothor-primary",
        correlation_id: str | None = None,
    ) -> None:
        self.run = AgentRun(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            agent_id=agent_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            correlation_id=correlation_id or str(uuid.uuid4()),
            status=RunStatus.PENDING,
        )
        self.messages: list[dict[str, Any]] = []
        self._step_counter = 0
        self._start_time: float | None = None

    @property
    def run_id(self) -> str:
        return self.run.id

    def start(
        self,
        system_prompt: str,
        user_message: str,
        tools_provided: list[str],
        delivery_mode: str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the session with system prompt and user message.

        If conversation_history is provided, prior messages are inserted
        between the system prompt and the current user message to give
        the LLM conversational context.
        """
        self.run.status = RunStatus.RUNNING
        self.run.started_at = datetime.now(UTC)
        self.run.system_prompt_chars = len(system_prompt)
        self.run.user_prompt_chars = len(user_message)
        self.run.tools_provided = tools_provided
        self.run.delivery_mode = delivery_mode
        self._start_time = time.monotonic()

        self.messages = [
            {"role": "system", "content": system_prompt},
            *(conversation_history or []),
            {"role": "user", "content": user_message},
        ]

    def record_llm_call(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_ms: int = 0,
        assistant_message: dict[str, Any] | None = None,
    ) -> RunStep:
        """Record an LLM API call step."""
        self._step_counter += 1
        step = RunStep(
            run_id=self.run_id,
            step_number=self._step_counter,
            step_type=StepType.LLM_CALL,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_ms=duration_ms,
        )
        self.run.steps.append(step)
        self.run.input_tokens += input_tokens
        self.run.output_tokens += output_tokens

        # Track model used
        if model and model not in self.run.models_attempted:
            self.run.models_attempted.append(model)
        if model:
            self.run.model_used = model

        # Append assistant message to conversation
        if assistant_message:
            self.messages.append(assistant_message)

        return step

    def record_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_output: dict[str, Any],
        tool_call_id: str,
        duration_ms: int = 0,
        error_message: str | None = None,
    ) -> RunStep:
        """Record a tool call + result step."""
        self._step_counter += 1
        step = RunStep(
            run_id=self.run_id,
            step_number=self._step_counter,
            step_type=StepType.TOOL_CALL,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_ms=duration_ms,
            error_message=error_message,
        )
        self.run.steps.append(step)

        # Append tool result to conversation
        import json

        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(tool_output, default=str),
            }
        )

        return step

    def record_error(self, error_message: str, traceback: str | None = None) -> RunStep:
        """Record an error step."""
        self._step_counter += 1
        step = RunStep(
            run_id=self.run_id,
            step_number=self._step_counter,
            step_type=StepType.ERROR,
            error_message=error_message,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        self.run.steps.append(step)
        return step

    def complete(self, output_text: str | None = None) -> AgentRun:
        """Mark the run as completed successfully."""
        self.run.status = RunStatus.COMPLETED
        self.run.completed_at = datetime.now(UTC)
        self.run.output_text = output_text
        if self._start_time:
            self.run.duration_ms = int((time.monotonic() - self._start_time) * 1000)
        return self.run

    def fail(self, error_message: str, traceback: str | None = None) -> AgentRun:
        """Mark the run as failed."""
        self.run.status = RunStatus.FAILED
        self.run.completed_at = datetime.now(UTC)
        self.run.error_message = error_message
        self.run.error_traceback = traceback
        if self._start_time:
            self.run.duration_ms = int((time.monotonic() - self._start_time) * 1000)
        return self.run

    def timeout(self) -> AgentRun:
        """Mark the run as timed out."""
        self.run.status = RunStatus.TIMEOUT
        self.run.completed_at = datetime.now(UTC)
        self.run.error_message = "Agent execution timed out"
        if self._start_time:
            self.run.duration_ms = int((time.monotonic() - self._start_time) * 1000)
        return self.run

    def check_budget(self, token_budget: int = 0, cost_budget_usd: float = 0.0) -> str:
        """Check if budget is exhausted, approaching, or ok.

        Returns: "exhausted", "warning", or "ok"
        """
        if token_budget > 0:
            total_tokens = self.run.input_tokens + self.run.output_tokens
            if total_tokens >= token_budget:
                return "exhausted"
            if total_tokens >= token_budget * 0.8:
                return "warning"
        if cost_budget_usd > 0:
            if self.run.total_cost_usd >= cost_budget_usd:
                return "exhausted"
            if self.run.total_cost_usd >= cost_budget_usd * 0.8:
                return "warning"
        return "ok"

    def get_final_text(self) -> str | None:
        """Extract the final assistant text from the conversation."""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                return str(msg["content"])
        return None
