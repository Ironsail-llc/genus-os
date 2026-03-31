"""
Agent Session — per-run message state and step recording.

Manages the conversation history for a single agent run:
- Tracks system prompt, user message, and all LLM/tool exchanges
- Records each step to the tracking DAL
- Accumulates token counts and cost
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from robothor.engine.models import AgentRun, RunStatus, RunStep, StepType, TriggerType

logger = logging.getLogger(__name__)


# Tools whose output contains untrusted external content — tagged for defense in depth
EXTERNAL_DATA_TOOLS: frozenset[str] = frozenset(
    {
        "web_fetch",
        "web_search",
        "search_memory",
        "get_entity",
        "get_conversation",
        "list_messages",
    }
)


class AgentSession:
    """Per-run state manager for an agent execution."""

    def __init__(
        self,
        agent_id: str,
        trigger_type: TriggerType = TriggerType.MANUAL,
        trigger_detail: str | None = None,
        tenant_id: str = "robothor-primary",
        correlation_id: str | None = None,
        tool_offload_threshold: int = 0,
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
        self._tool_offload_threshold = tool_offload_threshold

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
        content = json.dumps(tool_output, default=str)

        # Offload large results to temp file, keeping summary + path in context
        if self._tool_offload_threshold and len(content) > self._tool_offload_threshold:
            content = self._offload_tool_result(content, tool_name)

        # Wrap untrusted external data with tags so the LLM sees a boundary
        if tool_name in EXTERNAL_DATA_TOOLS:
            content = f'<untrusted_content source="{tool_name}">\n{content}\n</untrusted_content>'

        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
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

    def check_budget(self, token_budget: int = 0) -> str:
        """Check token budget status for observability and soft warnings.

        Returns: "exhausted", "warning", or "ok"

        Note: This is used for TRACKING and soft LLM nudges only.
        The engine does NOT enforce budget as a hard stop — runs
        continue regardless of budget status.
        """
        if token_budget > 0:
            total_tokens = self.run.input_tokens + self.run.output_tokens
            if total_tokens >= token_budget:
                return "exhausted"
            if total_tokens >= token_budget * 0.8:
                return "warning"
        return "ok"

    # ── Eager tool result compression ──────────────────────────────

    def _offload_tool_result(self, content: str, tool_name: str) -> str:
        """Write large tool result to temp file, return summary + file path."""
        from robothor.engine.compaction import extract_tool_summary

        summary = extract_tool_summary(content)
        fd, path = tempfile.mkstemp(prefix=f"tool_{tool_name}_", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return f"{summary}\n[Full output: {path} — use read_file to retrieve if needed]"

    def thin_previous_tool_results(self, protect_after_index: int) -> int:
        """Compress tool results from previous iterations to one-line summaries.

        Args:
            protect_after_index: Messages at or after this index keep full content.

        Returns:
            Characters saved.
        """
        from robothor.engine.compaction import TOOL_SUMMARY_MIN_CHARS, extract_tool_summary

        chars_saved = 0
        for i, msg in enumerate(self.messages):
            if i >= protect_after_index:
                break
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if len(content) < TOOL_SUMMARY_MIN_CHARS:
                continue
            summary = extract_tool_summary(content)
            if len(summary) < len(content):
                chars_saved += len(content) - len(summary)
                msg["content"] = summary
        return chars_saved

    def get_final_text(self) -> str | None:
        """Extract the final assistant text from the conversation.

        Handles both plain string content and list-of-blocks content
        (e.g. thinking + text blocks from extended thinking responses).
        """
        for msg in reversed(self.messages):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not content:
                continue
            if isinstance(content, list):
                text_parts = [
                    b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                ]
                return "\n".join(text_parts) if text_parts else None
            return str(content)
        return None
