"""
Data models for the Agent Engine.

All models are plain dataclasses — no ORM, no Pydantic. Matches the
frozen-dataclass pattern in robothor.config.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TriggerType(StrEnum):
    CRON = "cron"
    HOOK = "hook"
    EVENT = "event"
    MANUAL = "manual"
    TELEGRAM = "telegram"
    WEBCHAT = "webchat"
    WORKFLOW = "workflow"
    SUB_AGENT = "sub_agent"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class StepType(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    PLANNING = "planning"
    VERIFICATION = "verification"
    CHECKPOINT = "checkpoint"
    SCRATCHPAD = "scratchpad"
    ESCALATION = "escalation"
    GUARDRAIL = "guardrail"
    SPAWN_AGENT = "spawn_agent"


class DeliveryMode(StrEnum):
    ANNOUNCE = "announce"
    NONE = "none"
    LOG = "log"


@dataclass
class AgentHook:
    """Event hook — triggers an agent run when a matching Redis Stream event arrives."""

    stream: str  # Redis Stream name (e.g., "email", "calendar")
    event_type: str  # Event type filter (e.g., "email.new")
    message: str = ""  # Initial prompt sent to agent when triggered


@dataclass
class HeartbeatConfig:
    """Override configuration for periodic heartbeat runs.

    When attached to an AgentConfig, the scheduler creates a separate cron job
    that runs with these overrides (instruction file, delivery, warmup, etc.)
    while inheriting model + tools from the parent agent.
    """

    cron_expr: str = ""
    timezone: str = "America/New_York"
    instruction_file: str = ""
    session_target: str = "isolated"
    max_iterations: int = 15
    timeout_seconds: int = 600

    # Delivery (typically announce for heartbeat)
    delivery_mode: DeliveryMode = DeliveryMode.ANNOUNCE
    delivery_channel: str = ""
    delivery_to: str = ""

    # Warmup context for heartbeat runs
    warmup_context_files: list[str] = field(default_factory=list)
    warmup_peer_agents: list[str] = field(default_factory=list)
    warmup_memory_blocks: list[str] = field(default_factory=list)

    # Bootstrap files loaded into system prompt
    bootstrap_files: list[str] = field(default_factory=list)

    # Budget overrides
    token_budget: int = 0
    cost_budget_usd: float = 0.0


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
    timezone: str = "America/New_York"
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

    # Event hooks — triggers from Redis Streams (parsed from manifest hooks field)
    hooks: list[AgentHook] = field(default_factory=list)

    # Heartbeat — periodic health-check runs with overrides
    heartbeat: HeartbeatConfig | None = None

    # ── v2 enhancements (all default off for backward compat) ──
    # Sub-agent spawning
    can_spawn_agents: bool = False
    max_nesting_depth: int = 2  # absolute cap: 3
    sub_agent_max_iterations: int = 10
    sub_agent_timeout_seconds: int = 120

    error_feedback: bool = True
    token_budget: int = 0  # max tokens per run (0 = unlimited)
    cost_budget_usd: float = 0.0  # max cost per run (0 = unlimited)
    planning_enabled: bool = False
    planning_model: str = ""  # separate cheap model for planning
    scratchpad_enabled: bool = False
    guardrails: list[str] = field(default_factory=list)
    checkpoint_enabled: bool = False
    verification_enabled: bool = False
    verification_prompt: str = ""
    difficulty_class: str = ""  # simple, moderate, complex, or empty (auto)


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

    # v2 budget tracking
    token_budget: int = 0
    cost_budget_usd: float = 0.0
    budget_exhausted: bool = False

    # Sub-agent tracking
    parent_run_id: str | None = None
    nesting_depth: int = 0

    steps: list[RunStep] = field(default_factory=list)


@dataclass
class SpawnContext:
    """Context passed from parent agent to spawned child agents.

    Carries budget constraints, trace linkage, and nesting depth
    through the agent execution tree.
    """

    parent_run_id: str
    parent_agent_id: str
    correlation_id: str
    nesting_depth: int  # parent's depth (child = +1)
    max_nesting_depth: int = 2  # absolute cap: 3
    remaining_token_budget: int = 0
    remaining_cost_budget_usd: float = 0.0
    parent_trace_id: str = ""
    parent_span_id: str = ""


# ─── Workflow Engine Models ────────────────────────────────────────────


class WorkflowStepType(StrEnum):
    AGENT = "agent"
    TOOL = "tool"
    CONDITION = "condition"
    TRANSFORM = "transform"
    NOOP = "noop"


class WorkflowStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ConditionBranch:
    """A single branch in a condition step."""

    when: str | None = None  # Python expression (value = input)
    otherwise: bool = False
    goto: str = ""  # Step ID to jump to


@dataclass
class WorkflowStepDef:
    """Parsed step definition from workflow YAML."""

    id: str
    type: WorkflowStepType = WorkflowStepType.NOOP

    # Agent step
    agent_id: str = ""
    message: str = ""

    # Tool step
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)

    # Condition step
    input_expr: str = ""  # {{ steps.X.output_text }}
    branches: list[ConditionBranch] = field(default_factory=list)

    # Transform step
    transform_expr: str = ""

    # Error handling
    on_failure: str = "abort"  # abort, skip, retry
    retry_count: int = 0

    # Flow control
    next: str = ""  # Explicit next step ID (overrides sequential)


@dataclass
class WorkflowTriggerDef:
    """Trigger definition for a workflow."""

    type: str = ""  # hook, cron
    stream: str = ""
    event_type: str = ""
    cron: str = ""
    timezone: str = "America/New_York"


@dataclass
class WorkflowDef:
    """Complete workflow definition parsed from YAML."""

    id: str
    name: str = ""
    description: str = ""
    version: str = ""
    triggers: list[WorkflowTriggerDef] = field(default_factory=list)
    steps: list[WorkflowStepDef] = field(default_factory=list)
    timeout_seconds: int = 900
    delivery_mode: str = "none"
    delivery_channel: str = ""
    delivery_to: str = ""


@dataclass
class WorkflowStepResult:
    """Result of executing a single workflow step."""

    step_id: str
    step_type: WorkflowStepType = WorkflowStepType.NOOP
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING
    output_text: str | None = None
    agent_run_id: str | None = None
    tool_output: dict[str, Any] | None = None
    condition_branch: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class WorkflowRun:
    """Complete workflow execution."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    tenant_id: str = "robothor-primary"
    trigger_type: str = "manual"
    trigger_detail: str = ""
    correlation_id: str | None = None
    status: RunStatus = RunStatus.PENDING
    step_results: list[WorkflowStepResult] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    duration_ms: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
