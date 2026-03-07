"""Full-chain validation checks M-R — extends manifest checks A-L.

Validates workflow-chain-level concerns: event paths, pipeline continuity,
workflow coverage, tool-instruction coherence, tag flow, and delivery coherence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from robothor.templates.manifest_checks import CheckResult

# Known tool names for instruction file scanning
KNOWN_TOOLS = {
    "exec",
    "read_file",
    "write_file",
    "list_directory",
    "search_memory",
    "store_memory",
    "get_entity",
    "memory_block_read",
    "memory_block_write",
    "append_to_block",
    "memory_block_list",
    "create_task",
    "update_task",
    "get_task",
    "list_tasks",
    "list_my_tasks",
    "resolve_task",
    "delete_task",
    "create_note",
    "list_notes",
    "update_note",
    "delete_note",
    "create_person",
    "list_people",
    "get_person",
    "update_person",
    "create_company",
    "list_companies",
    "get_company",
    "update_company",
    "web_fetch",
    "web_search",
    "log_interaction",
    "list_conversations",
    "get_conversation",
    "list_messages",
    "create_message",
    "look",
    "who_is_here",
    "enroll_face",
    "set_vision_mode",
    "make_call",
    "list_agent_runs",
    "get_agent_stats",
    "get_fleet_health",
    "detect_anomalies",
    "git_status",
    "git_diff",
    "git_branch",
    "git_commit",
    "git_push",
    "create_pull_request",
    "spawn_agent",
    "spawn_agents",
    "vault_get",
    "vault_set",
    "vault_list",
    "send_message",
}

# Communication tools that imply delivery
COMMUNICATION_TOOLS = {"send_message", "create_message", "announce"}


def check_event_path(manifest: dict[str, Any], repo_root: Path) -> CheckResult:
    """M. If agent has hooks, verify a cron script publishes to that stream."""
    result = CheckResult("M", "Event path")
    hooks = manifest.get("hooks", [])
    if not hooks:
        return result.skip("No hooks defined")

    scripts_dir = repo_root / "brain" / "scripts"
    if not scripts_dir.is_dir():
        return result.skip("brain/scripts/ not found")

    # Collect all stream names from hooks
    expected_streams = set()
    for hook in hooks:
        if isinstance(hook, dict):
            stream = hook.get("stream", "")
            if stream:
                expected_streams.add(stream)

    if not expected_streams:
        return result.skip("No stream names in hooks")

    # Scan Python scripts for publish() calls
    found_streams: set[str] = set()
    for py_file in scripts_dir.glob("*.py"):
        try:
            content = py_file.read_text()
        except OSError:
            continue
        # Match patterns like publish("email" or publish('email' or stream="email"
        for stream in expected_streams:
            if re.search(rf"""['"]({re.escape(stream)})['"]""", content):
                found_streams.add(stream)

    missing = expected_streams - found_streams
    if missing:
        return result.warn(
            f"No cron script found publishing to stream(s): {sorted(missing)}",
            [f"Expected scripts in brain/scripts/*.py with publish() for: {s}" for s in missing],
        )
    return result


def check_pipeline_continuity(
    manifest: dict[str, Any], all_manifests: dict[str, Any]
) -> CheckResult:
    """N. If creates_tasks_for X, verify X receives_tasks_from this agent."""
    result = CheckResult("N", "Pipeline continuity")
    agent_id = manifest.get("id", "")
    creates_for = manifest.get("creates_tasks_for", [])
    if not creates_for:
        return result.skip("No creates_tasks_for defined")

    issues = []
    for target_id in creates_for:
        target = all_manifests.get(target_id)
        if not target:
            issues.append(f"Target '{target_id}' has no manifest")
            continue

        receives_from = target.get("receives_tasks_from", [])
        if agent_id not in receives_from:
            issues.append(f"'{target_id}' does not list '{agent_id}' in receives_tasks_from")

        if not target.get("task_protocol"):
            issues.append(f"'{target_id}' does not have task_protocol: true")

    if issues:
        return result.warn("Pipeline wiring incomplete", issues)
    return result


def check_workflow_coverage(manifest: dict[str, Any], repo_root: Path) -> CheckResult:
    """O. If agent appears in a workflow YAML, verify trigger matches."""
    result = CheckResult("O", "Workflow coverage")
    agent_id = manifest.get("id", "")
    workflows_dir = repo_root / "docs" / "workflows"

    if not workflows_dir.is_dir():
        return result.skip("docs/workflows/ not found")

    # Find workflows that reference this agent
    appearances: list[dict[str, Any]] = []
    for wf_file in workflows_dir.glob("*.yaml"):
        try:
            wf = yaml.safe_load(wf_file.read_text()) or {}
        except (yaml.YAMLError, OSError):
            continue

        appearances.extend(
            {"workflow": wf.get("id", wf_file.stem), "file": str(wf_file)}
            for step in wf.get("steps", [])
            if isinstance(step, dict) and step.get("agent") == agent_id
        )

    if not appearances:
        return result.skip("Agent not referenced in any workflow")

    # Check that agent has hooks or cron matching the workflow trigger
    issues: list[str] = []
    has_cron = bool(manifest.get("schedule", {}).get("cron"))
    has_hooks = bool(manifest.get("hooks"))

    if not has_cron and not has_hooks:
        issues.extend(
            f"Agent appears in workflow '{app['workflow']}' but has no cron or hooks"
            for app in appearances
        )

    if issues:
        return result.warn("Workflow trigger mismatch", issues)
    return result


def check_tool_instruction_coherence(manifest: dict[str, Any], repo_root: Path) -> CheckResult:
    """P. Parse instruction .md for tool references, verify in tools_allowed."""
    result = CheckResult("P", "Tool-instruction coherence")

    instr_file = manifest.get("instruction_file")
    if not instr_file:
        return result.skip("No instruction_file")

    instr_path = repo_root / instr_file
    if not instr_path.exists():
        return result.skip(f"Instruction file not found: {instr_file}")

    tools_allowed = set(manifest.get("tools_allowed", []))
    if not tools_allowed:
        return result.skip("No tools_allowed restriction (all tools available)")

    try:
        content = instr_path.read_text()
    except OSError:
        return result.skip(f"Cannot read instruction file: {instr_file}")

    # Find tool name references in the instruction file
    # Match tool_name patterns: function-call style, backtick-quoted, or bare references
    referenced_tools = set()
    for tool in KNOWN_TOOLS:
        # Match tool_name( or `tool_name` or tool_name()
        if re.search(rf"\b{re.escape(tool)}\b", content):
            referenced_tools.add(tool)

    missing = referenced_tools - tools_allowed
    if missing:
        return result.warn(
            f"Instruction references tools not in tools_allowed: {sorted(missing)}",
            [
                f"Tool '{t}' mentioned in instruction but not in manifest tools_allowed"
                for t in sorted(missing)
            ],
        )
    return result


def check_tag_flow(manifest: dict[str, Any], all_manifests: dict[str, Any]) -> CheckResult:
    """Q. If tags_produced is set, verify downstream has matching tags_consumed."""
    result = CheckResult("Q", "Tag flow")
    tags_produced = set(manifest.get("tags_produced", []))
    if not tags_produced:
        return result.skip("No tags_produced defined")

    # Collect all tags_consumed across all agents
    all_consumed: set[str] = set()
    consumers: dict[str, list[str]] = {}
    for other_id, other in all_manifests.items():
        consumed = set(other.get("tags_consumed", []))
        all_consumed.update(consumed)
        for tag in consumed:
            consumers.setdefault(tag, []).append(other_id)

    orphaned = tags_produced - all_consumed
    if orphaned:
        return result.warn(
            f"Produced tags with no consumer: {sorted(orphaned)}",
            [
                f"Tag '{t}' is produced but no agent has it in tags_consumed"
                for t in sorted(orphaned)
            ],
        )
    return result


def check_delivery_coherence(manifest: dict[str, Any], repo_root: Path) -> CheckResult:
    """R. If delivery: none but instruction mentions communication tools, warn."""
    result = CheckResult("R", "Delivery coherence")

    delivery_mode = manifest.get("delivery", {}).get("mode", "none")
    if delivery_mode != "none":
        return result.skip("Delivery mode is not 'none'")

    instr_file = manifest.get("instruction_file")
    if not instr_file:
        return result.skip("No instruction_file")

    instr_path = repo_root / instr_file
    if not instr_path.exists():
        return result.skip(f"Instruction file not found: {instr_file}")

    try:
        content = instr_path.read_text()
    except OSError:
        return result.skip(f"Cannot read instruction file: {instr_file}")

    found_comm_tools = [
        tool for tool in COMMUNICATION_TOOLS if re.search(rf"\b{re.escape(tool)}\b", content)
    ]

    if found_comm_tools:
        return result.warn(
            f"delivery: none but instruction references communication tools: {found_comm_tools}",
            ["Agent may be trying to send messages but delivery is disabled"],
        )
    return result


def validate_chain(
    manifest: dict[str, Any],
    all_manifests: dict[str, Any],
    repo_root: Path | None = None,
) -> list[CheckResult]:
    """Run all chain validation checks (M-R) for a single agent.

    Args:
        manifest: The agent manifest dict.
        all_manifests: All loaded manifests (for cross-references).
        repo_root: Repository root for file-based checks.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    return [
        check_event_path(manifest, repo_root),
        check_pipeline_continuity(manifest, all_manifests),
        check_workflow_coverage(manifest, repo_root),
        check_tool_instruction_coherence(manifest, repo_root),
        check_tag_flow(manifest, all_manifests),
        check_delivery_coherence(manifest, repo_root),
    ]
