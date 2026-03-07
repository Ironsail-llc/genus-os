"""Intent capture and build plan generation for agent creation.

Structured discovery before any files are created. Maps intent to the correct
orchestration pattern, tool profile, and model tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentIntent:
    """Captured intent for a new agent."""

    purpose: str = ""
    trigger_type: str = "cron"  # event, cron, interactive, sub-agent
    data_sources: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    upstream_agents: list[str] = field(default_factory=list)
    downstream_agents: list[str] = field(default_factory=list)
    complexity: str = "simple"  # simple, moderate, complex
    delivery: str = "none"  # none, announce, log
    existing_pipeline: str = ""  # name of pipeline this joins, if any
    requires_review: bool = False


@dataclass
class BuildPlan:
    """Complete build plan assembled from intent."""

    manifest: dict[str, Any] = field(default_factory=dict)
    instruction_skeleton: str = ""
    hook_config: list[dict[str, Any]] = field(default_factory=list)
    cron_script_suggestion: str = ""
    workflow_patch: dict[str, Any] | None = None
    tool_list: list[dict[str, Any]] = field(default_factory=list)
    model_tier: str = ""
    model_rationale: str = ""
    pattern: str = ""
    pattern_rationale: str = ""


# Pattern descriptions
PATTERNS = {
    "A": "Event-Driven Pipeline — cron publishes to Redis, hook triggers agent",
    "B": "Workflow Chain — YAML workflow chains sequential steps",
    "C": "Dynamic Sub-Agent Dispatch — parent spawns focused units at runtime",
    "D": "Cron Safety Net — hook is primary, cron is backup",
}

# Tool profiles
TOOL_PROFILES = {
    "read-only": {
        "description": "Monitors and analyzers — no writes",
        "tools": [
            "read_file",
            "search_memory",
            "get_entity",
            "memory_block_read",
            "web_fetch",
            "list_tasks",
            "list_my_tasks",
        ],
    },
    "crm-worker": {
        "description": "Task processors with CRM access",
        "tools": [
            "exec",
            "read_file",
            "write_file",
            "search_memory",
            "store_memory",
            "list_my_tasks",
            "update_task",
            "resolve_task",
            "create_task",
        ],
    },
    "action-unit": {
        "description": "Responders and communicators",
        "tools": [
            "exec",
            "read_file",
            "write_file",
            "web_fetch",
            "create_message",
            "log_interaction",
            "list_my_tasks",
            "update_task",
            "resolve_task",
        ],
    },
    "git-worker": {
        "description": "Code modification agents",
        "tools": [
            "exec",
            "read_file",
            "write_file",
            "git_status",
            "git_diff",
            "git_branch",
            "git_commit",
            "git_push",
            "create_pull_request",
            "list_my_tasks",
            "resolve_task",
        ],
    },
    "full-access": {
        "description": "Orchestrators and main agent",
        "tools": [],  # empty means all tools available
    },
}

# Model tier mapping
MODEL_TIERS = {
    "T0": {
        "label": "Router",
        "use_case": "Classification, triage, simple extraction",
        "variable": "model_primary",
    },
    "T1": {
        "label": "Worker",
        "use_case": "Standard tool use, CRM writes, file ops",
        "variable": "model_primary",
    },
    "T2": {
        "label": "Reasoning",
        "use_case": "Analysis, composition, complex decisions",
        "variable": "model_quality",
    },
    "T3": {
        "label": "Orchestrator",
        "use_case": "Multi-agent coordination, synthesis, code generation",
        "variable": "model_quality",
    },
}


def select_orchestration_pattern(intent: AgentIntent) -> str:
    """Map intent to the best orchestration pattern (A/B/C/D).

    Returns the pattern letter.
    """
    if intent.trigger_type == "sub-agent":
        return "C"

    if intent.trigger_type == "interactive":
        return "C"

    has_upstream = bool(intent.upstream_agents)
    has_downstream = bool(intent.downstream_agents)

    # If part of a sequential chain with explicit ordering, use workflow
    if has_upstream and has_downstream and intent.existing_pipeline:
        return "B"

    # Event-driven with a cron backup
    if intent.trigger_type == "event":
        return "D" if intent.data_sources else "A"

    # Pure cron agents that publish events for others
    if intent.trigger_type == "cron" and has_downstream:
        return "A"

    # Default: cron safety net pattern for event-capable agents
    if intent.trigger_type == "cron":
        return "D" if has_upstream else "A"

    return "A"


def select_tool_profile(intent: AgentIntent) -> list[str]:
    """Map intent to a tool profile, returning the tool list.

    Returns the list of tool names.
    """
    outputs_lower = [o.lower() for o in intent.outputs]
    sources_lower = [s.lower() for s in intent.data_sources]

    # Git workers
    if any(kw in " ".join(outputs_lower) for kw in ["pr", "pull request", "commit", "code"]):
        return list(TOOL_PROFILES["git-worker"]["tools"])

    # Action units (communication/messaging)
    if any(
        kw in " ".join(outputs_lower)
        for kw in ["email", "message", "reply", "call", "notification"]
    ):
        return list(TOOL_PROFILES["action-unit"]["tools"])

    # CRM workers (task-based processing)
    if intent.downstream_agents or any(
        kw in " ".join(outputs_lower) for kw in ["task", "update", "write"]
    ):
        tools = list(TOOL_PROFILES["crm-worker"]["tools"])
        # Add web tools if data sources suggest it
        all_text = " ".join(sources_lower + outputs_lower)
        if any(kw in all_text for kw in ["web", "url", "http", "api"]):
            for t in ["web_fetch", "web_search"]:
                if t not in tools:
                    tools.append(t)
        return tools

    # Read-only profile for monitors and analyzers
    if not intent.downstream_agents and not any(
        kw in " ".join(outputs_lower) for kw in ["write", "create", "send"]
    ):
        return list(TOOL_PROFILES["read-only"]["tools"])

    return list(TOOL_PROFILES["crm-worker"]["tools"])


def select_model_tier(intent: AgentIntent) -> tuple[str, str]:
    """Map complexity to model tier.

    Returns (model_variable, rationale).
    """
    if intent.complexity == "simple":
        tier = MODEL_TIERS["T0"]
        return tier["variable"], f"T0 ({tier['label']}): {tier['use_case']}"

    if intent.complexity == "moderate":
        tier = MODEL_TIERS["T1"]
        return tier["variable"], f"T1 ({tier['label']}): {tier['use_case']}"

    # complex
    if intent.trigger_type == "sub-agent" or intent.downstream_agents:
        tier = MODEL_TIERS["T3"]
        return tier["variable"], f"T3 ({tier['label']}): {tier['use_case']}"

    tier = MODEL_TIERS["T2"]
    return tier["variable"], f"T2 ({tier['label']}): {tier['use_case']}"


def generate_plan(intent: AgentIntent) -> BuildPlan:
    """Assemble a complete build plan from captured intent."""
    plan = BuildPlan()

    # Pattern
    plan.pattern = select_orchestration_pattern(intent)
    plan.pattern_rationale = PATTERNS.get(plan.pattern, "")

    # Tools
    tools = select_tool_profile(intent)
    plan.tool_list = [{"name": t, "rationale": "Selected by profile"} for t in tools]

    # Model
    model_var, model_rationale = select_model_tier(intent)
    plan.model_tier = model_var
    plan.model_rationale = model_rationale

    # Hooks
    if intent.trigger_type in ("event", "cron") and intent.data_sources:
        for source in intent.data_sources:
            stream = source.split(".")[0] if "." in source else source
            event_type = source if "." in source else f"{source}.new"
            plan.hook_config.append(
                {
                    "stream": stream,
                    "event_type": event_type,
                    "message": f"New {stream} data received. Process accordingly.",
                }
            )

    # Cron suggestion
    if plan.pattern in ("A", "D") and not plan.hook_config:
        plan.cron_script_suggestion = (
            "Consider creating a cron script that publishes events to Redis "
            "for this agent's data source."
        )

    # Workflow patch
    if plan.pattern == "B" and intent.existing_pipeline:
        plan.workflow_patch = {
            "pipeline": intent.existing_pipeline,
            "action": "append_step",
            "step": {"type": "agent", "agent": "", "on_failure": "skip"},
        }

    # Build manifest dict
    manifest: dict[str, Any] = {
        "id": "",
        "name": "",
        "description": intent.purpose,
        "version": "",
        "department": "custom",
        "reports_to": "main",
        "escalates_to": "main",
        "model": {"primary": f"{{{{ {model_var} }}}}"},
        "schedule": {
            "timezone": "{{ timezone }}",
            "timeout_seconds": 300,
            "max_iterations": 10,
            "session_target": "isolated",
        },
        "delivery": {"mode": intent.delivery},
        "tools_allowed": tools or [],
        "instruction_file": "",
        "bootstrap_files": [],
        "v2": {"error_feedback": True},
    }

    if intent.downstream_agents:
        manifest["creates_tasks_for"] = intent.downstream_agents
    if intent.upstream_agents:
        manifest["receives_tasks_from"] = intent.upstream_agents
        manifest["task_protocol"] = True

    if plan.hook_config:
        manifest["hooks"] = plan.hook_config

    if intent.requires_review:
        manifest["review_workflow"] = True

    # Cron schedule based on pattern
    if plan.pattern == "D":
        manifest["schedule"]["cron"] = "0 6-22/6 * * *"  # safety net
    elif plan.pattern in ("A", "B"):
        manifest["schedule"]["cron"] = "0 */2 * * *"

    plan.manifest = manifest

    # Instruction skeleton
    plan.instruction_skeleton = _build_instruction_skeleton(intent, manifest)

    return plan


def _build_instruction_skeleton(intent: AgentIntent, manifest: dict[str, Any]) -> str:
    """Build an instruction file skeleton from intent and manifest."""
    sections = []
    sections.append("# {agent_name}\n")
    sections.append("You are **{agent_name}**, an autonomous agent in the {{ai_name}} system.\n")
    sections.append("## Your Role\n")
    sections.append(f"{intent.purpose}\n")

    sections.append("## Tasks\n")
    task_num = 1
    if intent.upstream_agents:
        sections.append(
            f"{task_num}. Check your task inbox: "
            '`list_my_tasks(assignedToAgent="{agent_id}", status="TODO")`\n'
        )
        task_num += 1
    if intent.data_sources:
        for src in intent.data_sources:
            sections.append(f"{task_num}. Read data from {src}.\n")
            task_num += 1
    sections.append(f"{task_num}. Process the data according to your role.\n")
    task_num += 1
    if intent.downstream_agents:
        for downstream in intent.downstream_agents:
            sections.append(
                f'{task_num}. Create tasks for downstream: `create_task(assignedToAgent="{downstream}")`\n'
            )
            task_num += 1
    sections.append(f"{task_num}. Write your status file.\n")

    sections.append("\n## Output\n")
    sections.append("Write to `brain/memory/{agent_id}-status.md`:\n")
    sections.append("- One-line summary + ISO 8601 timestamp\n")

    if manifest.get("task_protocol"):
        sections.append("\n## Task Protocol\n")
        sections.append("1. `list_my_tasks()` to get pending tasks\n")
        sections.append('2. `update_task(id, status="IN_PROGRESS")` before processing\n')
        sections.append("3. Process the task\n")
        sections.append('4. `resolve_task(id, resolution="...")` when done\n')

    if manifest.get("review_workflow"):
        sections.append("\n## Review Workflow\n")
        sections.append("Set tasks to REVIEW (not DONE) when human approval is needed.\n")

    return "\n".join(sections)


def plan_to_scaffold(
    plan: BuildPlan,
    agent_id: str,
    agent_name: str,
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Write manifest + instruction file from a build plan.

    Returns dict mapping file type to file path written.
    """
    import yaml

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    # Fill in agent-specific fields
    manifest = dict(plan.manifest)
    manifest["id"] = agent_id
    manifest["name"] = agent_name
    manifest["instruction_file"] = f"brain/agents/{agent_id.upper().replace('-', '_')}.md"
    manifest["status_file"] = f"brain/memory/{agent_id}-status.md"

    # Write manifest
    manifest_path = repo_root / "docs" / "agents" / f"{agent_id}.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.dump(manifest, default_flow_style=False, sort_keys=False))

    # Write instruction file
    instr_content = plan.instruction_skeleton.replace("{agent_name}", agent_name).replace(
        "{agent_id}", agent_id
    )
    instr_path = repo_root / manifest["instruction_file"]
    instr_path.parent.mkdir(parents=True, exist_ok=True)
    instr_path.write_text(instr_content)

    return {
        "manifest": str(manifest_path),
        "instruction": str(instr_path),
    }
