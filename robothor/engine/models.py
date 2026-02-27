"""
Data models for the Agent Engine.

All models are plain dataclasses — no ORM, no Pydantic. Matches the
frozen-dataclass pattern in robothor.config.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TriggerType(str, Enum):
    CRON = "cron"
    HOOK = "hook"
    EVENT = "event"
    MANUAL = "manual"
    TELEGRAM = "telegram"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class StepType(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"


class DeliveryMode(str, Enum):
    ANNOUNCE = "announce"
    NONE = "none"
    LOG = "log"


@dataclass
class AgentConfig:
    """Configuration for a single agent, loaded from YAML manifest."""

    id: str
    name: str
    description: str = ""

    # Models
    model_primary: str = ""
    model_fallbacks: list[str] = field(default_factory=list)

    # Schedule
    cron_expr: str = ""
    timezone: str = "America/Grenada"
    timeout_seconds: int = 600
    session_target: str = "isolated"

    # Delivery
    delivery_mode: DeliveryMode = DeliveryMode.NONE
    delivery_channel: str = ""
    delivery_to: str = ""

    # Tools
    tools_allowed: list[str] = field(default_factory=list)
    tools_denied: list[str] = field(default_factory=list)

    # Instructions
    instruction_file: str = ""
    bootstrap_files: list[str] = field(default_factory=list)

    # Metadata
    reports_to: str = ""
    department: str = ""
    task_protocol: bool = False
    review_workflow: bool = False
    notification_inbox: bool = False
    shared_working_state: bool = False
    status_file: str = ""

    # SLA
    sla: dict[str, str] = field(default_factory=dict)

    # Streams
    streams_read: list[str] = field(default_factory=list)
    streams_write: list[str] = field(default_factory=list)

    # Warmup — pre-loaded context for cron/hook runs
    warmup_memory_blocks: list[str] = field(default_factory=list)
    warmup_context_files: list[str] = field(default_factory=list)
    warmup_peer_agents: list[str] = field(default_factory=list)

    # LLM parameters
    temperature: float = 0.3
    max_iterations: int = 20

    # Downstream agents to trigger after successful cron run
    downstream_agents: list[str] = field(default_factory=list)


@dataclass
class LLMMessage:
    """A single message in an LLM conversation."""

    role: str  # system, user, assistant, tool
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class RunStep:
    """A single step in an agent run (LLM call, tool call, or error)."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    step_number: int = 0
    step_type: StepType = StepType.LLM_CALL

    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None

    error_message: str | None = None


@dataclass
class AgentRun:
    """Represents a single agent execution attempt."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "robothor-primary"
    agent_id: str = ""

    trigger_type: TriggerType = TriggerType.MANUAL
    trigger_detail: str | None = None
    correlation_id: str | None = None

    status: RunStatus = RunStatus.PENDING

    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None

    model_used: str | None = None
    models_attempted: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0

    system_prompt_chars: int = 0
    user_prompt_chars: int = 0
    tools_provided: list[str] = field(default_factory=list)

    output_text: str | None = None
    error_message: str | None = None
    error_traceback: str | None = None

    delivery_mode: str | None = None
    delivery_status: str | None = None
    delivered_at: datetime | None = None
    delivery_channel: str | None = None

    steps: list[RunStep] = field(default_factory=list)
