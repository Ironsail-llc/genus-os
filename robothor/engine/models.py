"""
Data models for the Agent Engine.

All models are plain dataclasses — no ORM, no Pydantic. Matches the
frozen-dataclass pattern in robothor.config.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from datetime import datetime


class TriggerType(StrEnum):
    CRON = "cron"
    HOOK = "hook"
    EVENT = "event"
    MANUAL = "manual"
    TELEGRAM = "telegram"
    WEBCHAT = "webchat"
    SLACK = "slack"
    WORKFLOW = "workflow"
    SUB_AGENT = "sub_agent"
    FEDERATION = "federation"
    WEBHOOK = "webhook"
    IDE = "ide"
    CHANNEL_EVENT = "channel_event"  # Main wakes after fleet surfaces to the channel


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ErrorType(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NOT_FOUND = "not_found"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    API_DEPRECATED = "api_deprecated"
    LOGIC = "logic"
    UNKNOWN = "unknown"


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
    PLAN_PROPOSAL = "plan_proposal"
    REPLAN = "replan"
    ERROR_RECOVERY = "error_recovery"
    DEEP_REASON = "deep_reason"
    COMPACTION = "compaction"


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
    that runs with these overrides (instruction file, delivery, warmup, etc.).
    Model can be overridden per-beat; if not set, inherits from parent agent.
    """

    cron_expr: str = ""
    timezone: str = "America/New_York"
    # Optional model override for heartbeat runs. When empty, inherits from
    # the parent agent. Set to e.g. "openrouter/xiaomi/mimo-v2-pro" to pin
    # the beat to a specific (cheaper) model independent of the parent.
    model_primary: str = ""
    model_fallbacks: list[str] = field(default_factory=list)
    instruction_file: str = ""
    session_target: str = "isolated"
    max_iterations: int = 15  # soft check-in interval (not a hard cap)
    safety_cap: int = 50  # absolute max iterations for heartbeat runs
    # Wall-clock kills are off by default. Runs complete when the agent
    # finishes its work. Set a positive value only if you explicitly want
    # a circuit-breaker for this agent (not recommended).
    timeout_seconds: int = 0
    # Progress-based hang detector, off by default. When enabled, fires
    # only if no completed LLM response / tool call / stream content
    # arrives for this many seconds — i.e. a truly wedged provider.
    stall_timeout_seconds: int = 0

    # ── Cross-run persistent journal (multi-session research agents) ──
    journal_file: str = ""  # workspace-relative path to cross-run journal JSON
    journal_checkpoint_interval: int = 5  # write journal every N iterations (0 = end-of-run)
    resume_on_start: bool = False  # inject journal as startup preamble if exists

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
    cost_budget_usd: float = 0.0  # hard cost ceiling per run (0 = inherit from v2)

    # Persistent session history limit (messages to replay at the top of
    # each heartbeat run). Smaller = cheaper first LLM call; larger =
    # more continuity across beats. Default 20 matches the prior
    # hardcoded value in scheduler.py.
    persistent_history_limit: int = 20

    # Scout-specific tool restriction. When non-empty, overrides the parent
    # agent's `tools_allowed` for heartbeat runs only. The scout beat should
    # scan and file (create_task/update_task) — it must NOT spawn_agent or
    # execute work inside the beat. Leave empty to inherit parent's list.
    tools_allowed: list[str] = field(default_factory=list)

    # Override author identity for tasks created/updated during this beat.
    # When set, DAL writes `created_by_agent`/`updated_by_agent` to this
    # value instead of the run's agent_id. Lets the CRM timeline attribute
    # beat-filed tasks to "scout" while the underlying run_id stays "main".
    task_authorship_agent: str = ""


@dataclass
class WorkerConfig:
    """Override configuration for periodic drain/worker runs.

    Symmetric to HeartbeatConfig but for the drain cycle: main running every
    few hours with full tools to execute the tasks the scout filed. Shares
    the same agent identity ('main') but runs in a distinct session
    ('cron:main:worker') so it doesn't poison the scout's context.
    """

    cron_expr: str = ""
    timezone: str = "America/New_York"
    instruction_file: str = ""
    session_target: str = "persistent"
    max_iterations: int = 30
    safety_cap: int = 80
    timeout_seconds: int = 0
    stall_timeout_seconds: int = 0

    # Delivery (typically announce for drain so operator sees completions)
    delivery_mode: DeliveryMode = DeliveryMode.ANNOUNCE
    delivery_channel: str = ""
    delivery_to: str = ""

    # Warmup context for drain runs
    warmup_context_files: list[str] = field(default_factory=list)
    warmup_peer_agents: list[str] = field(default_factory=list)
    warmup_memory_blocks: list[str] = field(default_factory=list)

    # Bootstrap files loaded into system prompt
    bootstrap_files: list[str] = field(default_factory=list)

    # Budget overrides
    token_budget: int = 0
    cost_budget_usd: float = 0.0

    # Persistent session history limit
    persistent_history_limit: int = 20

    # Tool restriction for drain runs. When empty, inherits parent's full
    # `tools_allowed` — drain should have full execution capability.
    tools_allowed: list[str] = field(default_factory=list)


@dataclass
class ChannelBusConfig:
    """Main-agent-only — governs wake-on-surface behaviour.

    Non-main agents never consume this; the channel bus filters them out
    by authorship before a wake would ever reach them.
    """

    wake_on_surface: bool = False  # Off by default; flip on after observing Phase 1+2 clean
    wake_debounce_seconds: int = 15
    # Wake runs are not cost-gated. Rate limit + cooldown control
    # frequency; cost is observed but not enforced.
    wake_cost_budget_usd: float = 0.0
    per_agent_rate_limit_per_hour: int = 20
    main_wake_cooldown_seconds: int = 300  # at most one wake per 5 min
    wake_preamble_history_lines: int = 8


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
    catch_up: str = "coalesce"  # coalesce | skip_if_stale
    stale_after_minutes: int = 120

    # Delivery
    delivery_mode: DeliveryMode = DeliveryMode.NONE
    delivery_channel: str = ""
    delivery_to: str = ""
    # Channel bus: when an outbound delivery succeeds, dual-write it into main's
    # canonical session so main has visibility. Default on for fleet agents; main
    # is skipped at runtime by authorship filter.
    surface_to_channel: bool = True

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
    auto_task: bool = False  # Engine auto-creates/manages CRM task per run
    review_workflow: bool = False
    notification_inbox: bool = False
    shared_working_state: bool = False
    status_file: str = ""

    # SLA
    sla: dict[str, str] = field(default_factory=dict)

    # Goals — measurable success criteria for self-improvement loop
    goals: list[dict[str, Any]] = field(default_factory=list)

    # Streams
    streams_read: list[str] = field(default_factory=list)
    streams_write: list[str] = field(default_factory=list)

    # Warmup — pre-loaded context for cron/hook runs
    warmup_memory_blocks: list[str] = field(default_factory=list)
    warmup_context_files: list[str] = field(default_factory=list)
    warmup_peer_agents: list[str] = field(default_factory=list)

    # LLM parameters
    temperature: float = 0.3
    max_iterations: int = 20  # soft check-in interval (not a hard cap)
    safety_cap: int = 200  # absolute max iterations (infinite-loop protection only)
    # Progress-based hang detector, off by default. See HeartbeatConfig
    # for the semantics; stall_timeout fires only when no actual progress
    # (completed LLM response / tool / stream bytes) has occurred for
    # this many seconds. Not a "your run took too long" kill.
    stall_timeout_seconds: int = 0

    # ── Cross-run persistent journal (multi-session research agents) ──
    journal_file: str = ""  # workspace-relative path to cross-run journal JSON
    journal_checkpoint_interval: int = 5  # write journal every N iterations (0 = end-of-run)
    resume_on_start: bool = False  # inject journal as startup preamble if exists

    # How many prior messages to replay at the top of a session-resumed
    # cron/heartbeat run. Only honoured when session_target=persistent.
    persistent_history_limit: int = 20

    # Downstream agents to trigger after successful cron run
    downstream_agents: list[str] = field(default_factory=list)

    # Event hooks — triggers from Redis Streams (parsed from manifest hooks field)
    hooks: list[AgentHook] = field(default_factory=list)

    # Heartbeat — periodic health-check runs with overrides (the "scout beat")
    heartbeat: HeartbeatConfig | None = None

    # Task authorship override: when non-empty, tool handlers write this
    # identity to CRM `created_by_agent` / `updated_by_agent` instead of the
    # run's agent_id. Used so scout beats (running as agent_id='main') file
    # tasks attributed to 'scout' for CRM timeline clarity.
    task_author_override: str = ""

    # Worker — periodic queue-drain runs (the "drain cycle"). Symmetric to
    # heartbeat: same agent identity, different trigger, own session.
    worker: WorkerConfig | None = None

    # Channel bus — wake-on-surface (main only)
    channel_bus: ChannelBusConfig | None = None

    # ── v2 enhancements (all default off for backward compat) ──
    # Sub-agent spawning
    can_spawn_agents: bool = False
    max_nesting_depth: int = 2  # absolute cap: 3
    sub_agent_max_iterations: int = 10
    # Wall-clock cap for spawned child agents — 0 means no cap, child
    # runs until it finishes. Callers of spawn_agent may still pass an
    # explicit timeout_seconds argument for a narrowed task.
    sub_agent_timeout_seconds: int = 0
    max_concurrent_spawns: int = 0  # 0 = use engine default
    max_spawn_batch: int = 0  # 0 = use engine default

    # MCP client — external MCP servers agents can call
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)

    error_feedback: bool = True
    token_budget: int = 0  # token tracking (observability only, not enforced)
    planning_enabled: bool = False
    planning_model: str = ""  # separate cheap model for planning
    scratchpad_enabled: bool = False
    todo_list_enabled: bool = False  # In-conversation todo list (Claude Code-style)
    guardrails: list[str] = field(default_factory=list)
    guardrails_opt_out: bool = False  # Skip default guardrails for this agent
    exec_allowlist: list[str] = field(
        default_factory=list
    )  # regex patterns for allowed exec commands
    write_path_allowlist: list[str] = field(
        default_factory=list
    )  # glob patterns for allowed write paths
    checkpoint_enabled: bool = False
    verification_enabled: bool = False
    verification_prompt: str = ""
    difficulty_class: str = ""  # simple, moderate, complex, or empty (auto)
    lifecycle_hooks: list[dict[str, Any]] = field(default_factory=list)
    sandbox: str = "local"  # "local" or "docker"
    eager_tool_compression: bool = False  # disabled: infinite loop bug when read_file re-offloads
    tool_offload_threshold: int = 0  # disabled: 0 means no offloading

    # ── Tool execution ──
    tool_timeout_seconds: int = 120  # per-tool call timeout (0 = unlimited)

    # ── Continuous execution mode ──
    continuous: bool = False  # raises caps for sustained multi-hour runs
    progress_report_interval: int = 50  # iterations between Telegram progress updates
    max_cost_usd: float = 0.0  # dollar-cost cap (0 = unlimited)
    hard_budget: bool = False  # hard stop on budget exhaustion (vs soft nudge)

    # ── Human-in-the-loop (opt-in per agent) ──
    human_approval_tools: list[str] = field(
        default_factory=list
    )  # tool name patterns requiring approval
    human_approval_timeout: int = 300  # auto-approve after N seconds if no response

    # ── Config validation ──
    validation_warnings: list[str] = field(default_factory=list)


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
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None

    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None

    error_message: str | None = None


@dataclass
class AgentRun:
    """Represents a single agent execution attempt."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = field(default_factory=lambda: DEFAULT_TENANT)
    user_id: str = ""
    user_role: str = ""
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
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
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

    # Hierarchical tenant access (resolved at run start)
    accessible_tenant_ids: tuple[str, ...] = ()

    # CRM task linkage (auto-task)
    task_id: str | None = None

    # Contact 360 linkage — set when trigger_detail carries a chat_id we can
    # resolve via contact_identifiers, or inherited from a parent SpawnContext.
    person_id: str | None = None

    # Outcome assessment (interactive runs only)
    outcome_assessment: str | None = None  # "successful" | "partial" | "incorrect" | "abandoned"
    outcome_notes: str | None = None

    steps: list[RunStep] = field(default_factory=list)
    # Index of the first not-yet-persisted step. Bumped as the session
    # flushes steps mid-run so _persist_run_sync doesn't re-insert
    # already-committed rows (which would hit PK collisions).
    persisted_step_count: int = 0


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
    user_id: str = ""
    user_role: str = ""
    max_nesting_depth: int = 2  # absolute cap: 3
    max_spawn_batch: int = 0  # 0 = use engine default
    remaining_token_budget: int = 0
    remaining_cost_budget_usd: float = 0.0
    # Contact 360 linkage — propagates from parent run to all spawned children.
    person_id: str | None = None
    parent_trace_id: str = ""
    parent_span_id: str = ""
    # Stage 5 — CRM task this child is advancing. Set when a caller spawns
    # with parent_task_id. At run end, unfinished todo_write items are
    # lifted back to this task so the planner picks up next beat.
    parent_task_id: str | None = None


@dataclass
class RecoveryAction:
    """Describes how to recover from a classified error."""

    action: str  # "spawn", "retry", "backoff", "inject"
    agent_id: str = ""  # for spawn actions
    message: str = ""  # context message (spawn prompt or injection text)
    delay_seconds: int = 0  # for backoff actions


# ─── Plan Mode ────────────────────────────────────────────────────────

PLAN_TTL_SECONDS = 1800  # 30 minutes — stale plans auto-expire


@dataclass
class PlanState:
    """State of a pending plan awaiting approval.

    Created when an agent runs in plan mode (readonly tools only).
    The operator reviews the plan via Telegram inline keyboard or Helm approval card,
    then approves, rejects (with feedback), or iterates with text feedback.
    """

    plan_id: str
    plan_text: str  # Markdown plan the agent produced
    original_message: str  # User's original request
    status: str = "pending"  # pending | approved | rejected | expired | superseded
    created_at: str = ""  # ISO timestamp
    exploration_run_id: str = ""  # Run ID of the read-only phase
    rejection_feedback: str = ""  # Why the operator rejected (fed back to agent on re-plan)
    plan_hash: str = ""  # SHA-256 of plan_text for integrity verification on approval

    # Deep plan mode — when True, approval routes to execute_deep() instead of execute()
    deep_plan: bool = False

    # Iterative refinement
    revision_count: int = 0
    revision_history: list[dict[str, Any]] = field(
        default_factory=list
    )  # [{plan_text, feedback, timestamp}]

    # Execution tracking
    execution_run_id: str = ""  # Run ID of the execution phase (after approval)


# ─── Deep Mode ─────────────────────────────────────────────────────────


@dataclass
class DeepRunState:
    """State of an active /deep reasoning session.

    Created when a user invokes /deep from any surface.  The RLM runs
    synchronously in a background thread; progress is pushed to UIs
    via elapsed-time heartbeats.
    """

    deep_id: str
    query: str
    status: str = "running"  # running | completed | failed
    started_at: str = ""
    completed_at: str = ""
    response: str = ""
    execution_time_s: float = 0.0
    cost_usd: float = 0.0
    context_chars: int = 0
    trajectory_file: str = ""
    error: str = ""


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
    tenant_id: str = field(default_factory=lambda: DEFAULT_TENANT)
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
