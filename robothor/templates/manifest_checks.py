"""
Agent manifest validation checks A-M — extracted from scripts/validate_agents.py.

These are the 13 checks that validate an agent manifest against the schema contract.
Both the standalone script and the template installer call these checks.

Usage:
    from robothor.templates.manifest_checks import validate_agent, CheckResult
    results = validate_agent(manifest, all_manifests, registered_tools)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    CronTrigger = None

# Valid enum values
VALID_DELIVERY_MODES = {"announce", "none", "log"}
VALID_SESSION_TARGETS = {"isolated", "persistent"}

# Tools that agents with status_file MUST have
FILE_WRITE_TOOLS = {"exec", "write_file"}

# Basic tools every agent should have for file I/O
BASIC_IO_TOOLS = {"exec", "read_file", "write_file"}


class CheckResult:
    """Result of a single validation check."""

    def __init__(self, check_id: str, name: str):
        self.check_id = check_id
        self.name = name
        self.status = "PASS"
        self.message = ""
        self.details: list[str] = []

    def fail(self, msg: str, details: list[str] | None = None) -> CheckResult:
        self.status = "FAIL"
        self.message = msg
        self.details = details or []
        return self

    def warn(self, msg: str, details: list[str] | None = None) -> CheckResult:
        self.status = "WARN"
        self.message = msg
        self.details = details or []
        return self

    def skip(self, msg: str) -> CheckResult:
        self.status = "SKIP"
        self.message = msg
        return self


def load_schema(schema_path: Path) -> tuple[dict[str, Any], set[str], set[str]]:
    """Load the agent manifest schema.

    Returns (schema_dict, required_fields, department_enums).
    """
    import yaml

    if schema_path.exists():
        with schema_path.open() as f:
            schema = yaml.safe_load(f) or {}
        required_fields = set(schema.get("required", {}).keys())
        dept_info = schema.get("required", {}).get("department", {})
        departments = set(dept_info.get("enum", []))
        return schema, required_fields, departments
    return {}, {"id", "name"}, set()


def check_schema_required(
    manifest: dict[str, Any],
    required_fields: set[str],
    departments: set[str],
) -> CheckResult:
    """A. Schema required fields -- strict, blocks commit."""
    result = CheckResult("A", "Schema required fields")
    issues: list[str] = []

    required = required_fields or {"id", "name"}
    issues.extend(
        f"Missing required field: {field}"
        for field in required
        if field not in manifest or not manifest[field]
    )

    agent_id = manifest.get("id", "")
    if agent_id and not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", agent_id):
        issues.append(f"id '{agent_id}' is not kebab-case")

    dept = manifest.get("department", "")
    if departments and dept and dept not in departments:
        issues.append(f"department '{dept}' not in schema enum: {sorted(departments)}")

    if issues:
        return result.fail("Schema violations (required)", issues)
    return result


def check_structure(manifest: dict[str, Any]) -> CheckResult:
    """B. Manifest structure -- delivery, session, model enums."""
    result = CheckResult("B", "Manifest structure")
    issues = []

    delivery_mode = manifest.get("delivery", {}).get("mode", "none")
    if delivery_mode not in VALID_DELIVERY_MODES:
        issues.append(f"Invalid delivery.mode: {delivery_mode}")

    session_target = manifest.get("schedule", {}).get("session_target", "isolated")
    if session_target not in VALID_SESSION_TARGETS:
        issues.append(f"Invalid session_target: {session_target}")

    if delivery_mode == "announce":
        if not manifest.get("delivery", {}).get("channel"):
            issues.append("delivery.mode=announce but no delivery.channel")
        if not manifest.get("delivery", {}).get("to"):
            issues.append("delivery.mode=announce but no delivery.to")

    if not manifest.get("model", {}).get("primary"):
        issues.append("No model.primary specified")

    if issues:
        return result.fail("Structure issues", issues)
    return result


def check_files(manifest: dict[str, Any], repo_root: Path) -> CheckResult:
    """C. Instruction + bootstrap file existence."""
    result = CheckResult("C", "File existence")
    issues = []

    instr_file = manifest.get("instruction_file")
    if instr_file and instr_file != "null":
        full_path = repo_root / instr_file
        if not full_path.exists():
            issues.append(f"instruction_file not found: {instr_file}")
    elif not instr_file or instr_file == "null":
        agent_id = manifest.get("id", "?")
        issues.append(f"No instruction_file for {agent_id} (agent runs on bootstrap only)")

    for bf in manifest.get("bootstrap_files", []):
        full_path = repo_root / bf
        if not full_path.exists():
            issues.append(f"bootstrap_file not found: {bf}")

    if issues:
        has_missing = any("not found" in i for i in issues)
        if has_missing:
            return result.fail("Missing files", issues)
        return result.warn("File issues", issues)
    return result


def check_tools_registered(manifest: dict[str, Any], registered: set[str]) -> CheckResult:
    """D. tools_allowed entries are registered in ToolRegistry."""
    result = CheckResult("D", "Tools registered")
    if not registered:
        return result.skip("ToolRegistry not available")

    tools_allowed = manifest.get("tools_allowed", [])
    if not tools_allowed:
        return result

    unknown = [t for t in tools_allowed if t not in registered]
    if unknown:
        return result.fail(f"Unknown tools in tools_allowed: {unknown}")
    return result


def check_status_file_tools(manifest: dict[str, Any]) -> CheckResult:
    """E. Agents with status_file have file write tools."""
    result = CheckResult("E", "Status file tools")
    status_file = manifest.get("status_file")
    if not status_file:
        return result.skip("No status_file declared")

    tools_allowed = set(manifest.get("tools_allowed", []))
    if not tools_allowed:
        return result

    has_write = tools_allowed & FILE_WRITE_TOOLS
    if not has_write:
        return result.fail(
            "Agent has status_file but no write tools",
            [f"status_file: {status_file}", f"Need one of: {sorted(FILE_WRITE_TOOLS)}"],
        )
    return result


def check_cron(manifest: dict[str, Any]) -> CheckResult:
    """F. Cron expression validity."""
    result = CheckResult("F", "Cron expression")
    cron_expr = manifest.get("schedule", {}).get("cron", "")
    if not cron_expr:
        return result.skip("No cron expression")

    if CronTrigger is None:
        return result.skip("APScheduler not available for validation")

    try:
        CronTrigger.from_crontab(cron_expr)
    except Exception as e:
        return result.fail(f"Invalid cron expression '{cron_expr}': {e}")
    return result


def check_relationships(manifest: dict[str, Any], all_manifests: dict[str, Any]) -> CheckResult:
    """G. Relationship targets reference valid agent IDs."""
    result = CheckResult("G", "Relationships")
    issues: list[str] = []

    for field in ["creates_tasks_for", "receives_tasks_from"]:
        issues.extend(
            f"{field} target '{target}' has no manifest"
            for target in manifest.get(field, [])
            if target not in all_manifests
        )

    reports_to = manifest.get("reports_to")
    if reports_to and reports_to not in all_manifests:
        issues.append(f"reports_to '{reports_to}' has no manifest")

    escalates_to = manifest.get("escalates_to")
    if escalates_to and escalates_to not in all_manifests:
        issues.append(f"escalates_to '{escalates_to}' has no manifest")

    if issues:
        return result.warn("Relationship targets incomplete", issues)
    return result


def check_permission_coherence(manifest: dict[str, Any]) -> CheckResult:
    """H. No tool in both allowed AND denied."""
    result = CheckResult("H", "Permission coherence")
    allowed = set(manifest.get("tools_allowed", []))
    denied = set(manifest.get("tools_denied", []))
    overlap = allowed & denied

    if overlap:
        return result.warn(f"Tools in both allowed and denied: {sorted(overlap)}")
    return result


def check_downstream(manifest: dict[str, Any], all_manifests: dict[str, Any]) -> CheckResult:
    """I. Downstream agents reference valid IDs."""
    result = CheckResult("I", "Downstream agents")
    downstream = manifest.get("downstream_agents", [])
    if not downstream:
        return result.skip("No downstream agents")

    invalid = [d for d in downstream if d not in all_manifests]
    if invalid:
        return result.fail(f"Unknown downstream agents: {invalid}")
    return result


def check_warmup_files(manifest: dict[str, Any], repo_root: Path) -> CheckResult:
    """J. Warmup context_files exist on disk."""
    result = CheckResult("J", "Warmup files")
    warmup = manifest.get("warmup", {})
    context_files = warmup.get("context_files", [])
    if not context_files:
        return result.skip("No warmup context_files")

    missing = []
    for cf in context_files:
        full_path = repo_root / cf
        if not full_path.exists():
            missing.append(cf)

    if missing:
        return result.warn(f"Warmup files not found (may be created at runtime): {missing}")
    return result


def check_basic_io_tools(manifest: dict[str, Any]) -> CheckResult:
    """K. Agents with tools_allowed should include basic I/O tools."""
    result = CheckResult("K", "Basic I/O tools")
    tools_allowed = set(manifest.get("tools_allowed", []))
    if not tools_allowed:
        return result.skip("No tools_allowed (all tools available)")

    missing = BASIC_IO_TOOLS - tools_allowed
    if missing:
        return result.warn(
            f"Missing basic I/O tools: {sorted(missing)}",
            ["Agents typically need exec, read_file, write_file for file operations"],
        )
    return result


def check_hooks(manifest: dict[str, Any]) -> CheckResult:
    """L. Hooks entries are well-formed (stream, event_type required)."""
    result = CheckResult("L", "Hooks validity")
    hooks = manifest.get("hooks", [])
    if not hooks:
        return result.skip("No hooks defined")

    if not isinstance(hooks, list):
        return result.fail("hooks must be a list")

    issues = []
    for i, hook in enumerate(hooks):
        if not isinstance(hook, dict):
            issues.append(f"hooks[{i}]: not a dict")
            continue
        if not hook.get("stream"):
            issues.append(f"hooks[{i}]: missing 'stream'")
        if not hook.get("event_type"):
            issues.append(f"hooks[{i}]: missing 'event_type'")

    if issues:
        return result.fail("Invalid hook entries", issues)
    return result


def check_secret_refs(manifest: dict[str, Any]) -> CheckResult:
    """M. SecretRef keys are present in the environment."""
    result = CheckResult("M", "Secret references")
    secret_refs = manifest.get("secret_refs", [])
    if not secret_refs:
        return result.skip("No secret_refs declared")

    if not isinstance(secret_refs, list):
        return result.fail("secret_refs must be a list")

    import os

    missing = [key for key in secret_refs if not os.environ.get(key)]
    if missing:
        return result.warn(
            f"Secret keys not in environment: {missing}",
            ["Keys may be loaded at runtime via EnvironmentFile — check secrets.env"],
        )
    return result


def validate_agent(
    manifest: dict[str, Any],
    all_manifests: dict[str, Any],
    registered_tools: set[str],
    repo_root: Path | None = None,
    ci: bool = False,
) -> list[CheckResult]:
    """Run all 13 checks (A-M) for a single agent.

    Args:
        manifest: The agent manifest dict.
        all_manifests: All loaded manifests (for cross-references).
        registered_tools: Tool names from Engine ToolRegistry.
        repo_root: Repository root for file existence checks.
        ci: When True, skip checks that require local symlinks (C, J).
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    # Load schema for check A
    schema_path = repo_root / "docs" / "agents" / "schema.yaml"
    _, required_fields, departments = load_schema(schema_path)

    checks = [
        check_schema_required(manifest, required_fields, departments),
        check_structure(manifest),
    ]

    if ci:
        skip_c = CheckResult("C", "File existence")
        skip_c.skip("Skipped in CI (symlinks not available)")
        checks.append(skip_c)
    else:
        checks.append(check_files(manifest, repo_root))

    checks.extend(
        [
            check_tools_registered(manifest, registered_tools),
            check_status_file_tools(manifest),
            check_cron(manifest),
            check_relationships(manifest, all_manifests),
            check_permission_coherence(manifest),
            check_downstream(manifest, all_manifests),
        ]
    )

    if ci:
        skip_j = CheckResult("J", "Warmup files")
        skip_j.skip("Skipped in CI (symlinks not available)")
        checks.append(skip_j)
    else:
        checks.append(check_warmup_files(manifest, repo_root))

    checks.extend(
        [
            check_basic_io_tools(manifest),
            check_hooks(manifest),
            check_secret_refs(manifest),
        ]
    )

    return checks
