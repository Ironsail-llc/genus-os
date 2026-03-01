#!/usr/bin/env python3
"""Validate agent manifests against the schema contract.

Loads docs/agents/schema.yaml as the source of truth and enforces:
  A. Schema required fields (strict — blocks commit)
  B. Manifest structure (delivery, session_target, model enums)
  C. Instruction + bootstrap file existence
  D. tools_allowed entries registered in ToolRegistry
  E. Agents with status_file have file write tools (exec/write_file)
  F. Cron expression validity
  G. Relationship targets reference valid agent IDs
  H. Permission coherence (no tool in both allowed AND denied)
  I. Downstream agents reference valid IDs
  J. Warmup file existence (context_files)
  K. Basic I/O tools
  L. Hooks validity (stream, event_type, message required per entry)

Usage:
    python scripts/validate_agents.py                   # Check all agents
    python scripts/validate_agents.py --agent <id>      # Check one agent
    python scripts/validate_agents.py --verbose          # Show details
    python scripts/validate_agents.py --json             # JSON output
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / "docs" / "agents"
SCHEMA_PATH = MANIFEST_DIR / "schema.yaml"

# Valid enum values
VALID_DELIVERY_MODES = {"announce", "none", "log"}
VALID_SESSION_TARGETS = {"isolated", "persistent"}

# Loaded from schema.yaml at runtime
SCHEMA: dict = {}
SCHEMA_REQUIRED_FIELDS: set[str] = set()
SCHEMA_DEPARTMENTS: set[str] = set()


def load_schema() -> dict:
    """Load the agent manifest schema from docs/agents/schema.yaml."""
    global SCHEMA, SCHEMA_REQUIRED_FIELDS, SCHEMA_DEPARTMENTS
    if SCHEMA_PATH.exists():
        with open(SCHEMA_PATH) as f:
            SCHEMA = yaml.safe_load(f) or {}
        SCHEMA_REQUIRED_FIELDS = set(SCHEMA.get("required", {}).keys())
        dept_info = SCHEMA.get("required", {}).get("department", {})
        SCHEMA_DEPARTMENTS = set(dept_info.get("enum", []))
    else:
        # Fallback if schema file is missing
        SCHEMA_REQUIRED_FIELDS = {"id", "name"}
        SCHEMA_DEPARTMENTS = set()
    return SCHEMA


# Kept for backward compat — now derived from schema
REQUIRED_MANIFEST_FIELDS = {"id", "name"}

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

    def fail(self, msg: str, details: list[str] | None = None):
        self.status = "FAIL"
        self.message = msg
        self.details = details or []
        return self

    def warn(self, msg: str, details: list[str] | None = None):
        self.status = "WARN"
        self.message = msg
        self.details = details or []
        return self

    def skip(self, msg: str):
        self.status = "SKIP"
        self.message = msg
        return self


def load_manifests(agent_id: str | None = None) -> dict:
    """Load YAML manifests from docs/agents/."""
    manifests = {}
    for f in sorted(MANIFEST_DIR.glob("*.yaml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
        if (
            data
            and isinstance(data, dict)
            and "id" in data
            and (agent_id is None or data["id"] == agent_id)
        ):
            manifests[data["id"]] = data
    return manifests


def get_registered_tools() -> set[str]:
    """Get all tool names registered in the Engine ToolRegistry."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from robothor.engine.tools import ToolRegistry

        registry = ToolRegistry()
        return set(registry._schemas.keys())
    except Exception as e:
        print(f"WARNING: Cannot load ToolRegistry: {e}", file=sys.stderr)
        return set()


def check_schema_required(manifest: dict) -> CheckResult:
    """A. Schema required fields — strict, blocks commit."""
    result = CheckResult("A", "Schema required fields")
    issues = []

    # Check required fields from schema.yaml
    required = SCHEMA_REQUIRED_FIELDS or {"id", "name"}
    for field in required:
        if field not in manifest or not manifest[field]:
            issues.append(f"Missing required field: {field}")

    # Validate id is kebab-case
    agent_id = manifest.get("id", "")
    if agent_id and not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", agent_id):
        issues.append(f"id '{agent_id}' is not kebab-case")

    # Validate department enum
    dept = manifest.get("department", "")
    if SCHEMA_DEPARTMENTS and dept and dept not in SCHEMA_DEPARTMENTS:
        issues.append(f"department '{dept}' not in schema enum: {sorted(SCHEMA_DEPARTMENTS)}")

    if issues:
        return result.fail("Schema violations (required)", issues)
    return result


def check_structure(manifest: dict) -> CheckResult:
    """B. Manifest structure — delivery, session, model enums."""
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


def check_files(manifest: dict) -> CheckResult:
    """C. Instruction + bootstrap file existence."""
    result = CheckResult("C", "File existence")
    issues = []

    instr_file = manifest.get("instruction_file")
    if instr_file and instr_file != "null":
        full_path = REPO_ROOT / instr_file
        if not full_path.exists():
            issues.append(f"instruction_file not found: {instr_file}")
    elif not instr_file or instr_file == "null":
        # No instruction file — warn but don't fail
        agent_id = manifest.get("id", "?")
        issues.append(f"No instruction_file for {agent_id} (agent runs on bootstrap only)")

    for bf in manifest.get("bootstrap_files", []):
        full_path = REPO_ROOT / bf
        if not full_path.exists():
            issues.append(f"bootstrap_file not found: {bf}")

    if issues:
        # File-not-found is FAIL, missing instruction_file is WARN
        has_missing = any("not found" in i for i in issues)
        if has_missing:
            return result.fail("Missing files", issues)
        return result.warn("File issues", issues)
    return result


def check_tools_registered(manifest: dict, registered: set[str]) -> CheckResult:
    """D. tools_allowed entries are registered in ToolRegistry."""
    result = CheckResult("D", "Tools registered")
    if not registered:
        return result.skip("ToolRegistry not available")

    tools_allowed = manifest.get("tools_allowed", [])
    if not tools_allowed:
        return result  # No allowlist = all tools available

    unknown = [t for t in tools_allowed if t not in registered]
    if unknown:
        return result.fail(f"Unknown tools in tools_allowed: {unknown}")
    return result


def check_status_file_tools(manifest: dict) -> CheckResult:
    """E. Agents with status_file have file write tools."""
    result = CheckResult("E", "Status file tools")
    status_file = manifest.get("status_file")
    if not status_file:
        return result.skip("No status_file declared")

    tools_allowed = set(manifest.get("tools_allowed", []))
    if not tools_allowed:
        return result  # No allowlist = all tools available

    has_write = tools_allowed & FILE_WRITE_TOOLS
    if not has_write:
        return result.fail(
            "Agent has status_file but no write tools",
            [f"status_file: {status_file}", f"Need one of: {sorted(FILE_WRITE_TOOLS)}"],
        )
    return result


def check_cron(manifest: dict) -> CheckResult:
    """F. Cron expression validity."""
    result = CheckResult("F", "Cron expression")
    cron_expr = manifest.get("schedule", {}).get("cron", "")
    if not cron_expr:
        return result.skip("No cron expression")

    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron_expr)
    except ImportError:
        return result.skip("APScheduler not available for validation")
    except Exception as e:
        return result.fail(f"Invalid cron expression '{cron_expr}': {e}")
    return result


def check_relationships(manifest: dict, all_manifests: dict) -> CheckResult:
    """G. Relationship targets reference valid agent IDs."""
    result = CheckResult("G", "Relationships")
    issues = []

    for field in ["creates_tasks_for", "receives_tasks_from"]:
        for target in manifest.get(field, []):
            if target not in all_manifests:
                issues.append(f"{field} target '{target}' has no manifest")

    reports_to = manifest.get("reports_to")
    if reports_to and reports_to not in all_manifests:
        issues.append(f"reports_to '{reports_to}' has no manifest")

    escalates_to = manifest.get("escalates_to")
    if escalates_to and escalates_to not in all_manifests:
        issues.append(f"escalates_to '{escalates_to}' has no manifest")

    if issues:
        return result.warn("Relationship targets incomplete", issues)
    return result


def check_permission_coherence(manifest: dict) -> CheckResult:
    """H. No tool in both allowed AND denied."""
    result = CheckResult("H", "Permission coherence")
    allowed = set(manifest.get("tools_allowed", []))
    denied = set(manifest.get("tools_denied", []))
    overlap = allowed & denied

    if overlap:
        return result.warn(f"Tools in both allowed and denied: {sorted(overlap)}")
    return result


def check_downstream(manifest: dict, all_manifests: dict) -> CheckResult:
    """I. Downstream agents reference valid IDs."""
    result = CheckResult("I", "Downstream agents")
    downstream = manifest.get("downstream_agents", [])
    if not downstream:
        return result.skip("No downstream agents")

    invalid = [d for d in downstream if d not in all_manifests]
    if invalid:
        return result.fail(f"Unknown downstream agents: {invalid}")
    return result


def check_warmup_files(manifest: dict) -> CheckResult:
    """J. Warmup context_files exist on disk."""
    result = CheckResult("J", "Warmup files")
    warmup = manifest.get("warmup", {})
    context_files = warmup.get("context_files", [])
    if not context_files:
        return result.skip("No warmup context_files")

    missing = []
    for cf in context_files:
        full_path = REPO_ROOT / cf
        if not full_path.exists():
            missing.append(cf)

    if missing:
        return result.warn(f"Warmup files not found (may be created at runtime): {missing}")
    return result


def check_basic_io_tools(manifest: dict) -> CheckResult:
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


def check_hooks(manifest: dict) -> CheckResult:
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


def validate_agent(
    manifest: dict,
    all_manifests: dict,
    registered_tools: set[str],
    ci: bool = False,
) -> list[CheckResult]:
    """Run all checks for a single agent.

    Args:
        ci: When True, skip checks that require local symlinks (C, J).
    """
    checks = [
        check_schema_required(manifest),
        check_structure(manifest),
    ]
    if ci:
        skip_c = CheckResult("C", "File existence")
        skip_c.skip("Skipped in CI (symlinks not available)")
        checks.append(skip_c)
    else:
        checks.append(check_files(manifest))
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
        checks.append(check_warmup_files(manifest))
    checks.extend(
        [
            check_basic_io_tools(manifest),
            check_hooks(manifest),
        ]
    )
    return checks


def main():
    parser = argparse.ArgumentParser(description="Validate agent manifests against the Engine")
    parser.add_argument("--agent", "-a", help="Check a single agent by ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show details")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: skip checks requiring local symlinks (C, J)",
    )
    args = parser.parse_args()

    # Load schema
    schema = load_schema()
    if schema:
        print(f"Schema: {len(SCHEMA_REQUIRED_FIELDS)} required fields loaded from schema.yaml")
    else:
        print("Schema: schema.yaml not found, using minimal required fields")

    # Load all manifests
    all_manifests = load_manifests()
    if not all_manifests:
        print("ERROR: No manifests found in docs/agents/*.yaml", file=sys.stderr)
        sys.exit(2)

    # Load registered tools from Engine
    registered_tools = get_registered_tools()
    if registered_tools:
        print(f"ToolRegistry: {len(registered_tools)} tools registered")
    else:
        print("ToolRegistry: unavailable (tool checks will be skipped)")

    # Filter to single agent if specified
    if args.agent:
        target = load_manifests(args.agent)
        if not target:
            print(f"ERROR: No manifest found for '{args.agent}'", file=sys.stderr)
            sys.exit(1)
    else:
        target = all_manifests

    # Run validation
    all_results = {}
    total_pass = 0
    total_warn = 0
    total_fail = 0
    total_skip = 0

    for agent_id, manifest in sorted(target.items()):
        results = validate_agent(manifest, all_manifests, registered_tools, ci=args.ci)
        all_results[agent_id] = results

        total_pass += sum(1 for r in results if r.status == "PASS")
        total_warn += sum(1 for r in results if r.status == "WARN")
        total_fail += sum(1 for r in results if r.status == "FAIL")
        total_skip += sum(1 for r in results if r.status == "SKIP")

    # JSON output
    if args.json:
        output = {}
        for agent_id, results in all_results.items():
            output[agent_id] = [
                {
                    "check": r.check_id,
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                    "details": r.details,
                }
                for r in results
            ]
        json.dump(
            {
                "agents": output,
                "summary": {
                    "total_agents": len(all_results),
                    "total_checks": total_pass + total_warn + total_fail + total_skip,
                    "pass": total_pass,
                    "warn": total_warn,
                    "fail": total_fail,
                    "skip": total_skip,
                },
            },
            sys.stdout,
            indent=2,
        )
        print()
        sys.exit(1 if total_fail > 0 else 0)

    # Human-readable output
    print()
    print("=== Agent Fleet Validation ===")
    print(f"Manifests: docs/agents/*.yaml ({len(all_manifests)} total)")
    print()

    for agent_id, results in sorted(all_results.items()):
        passes = sum(1 for r in results if r.status == "PASS")
        warns = sum(1 for r in results if r.status == "WARN")
        fails = sum(1 for r in results if r.status == "FAIL")

        status_parts = []
        if passes:
            status_parts.append(f"{passes} PASS")
        if warns:
            status_parts.append(f"{warns} WARN")
        if fails:
            status_parts.append(f"{fails} FAIL")

        dots = "." * max(1, 40 - len(agent_id))
        print(f"{agent_id} {dots} {', '.join(status_parts)}")

        if args.verbose or fails > 0:
            for r in results:
                if r.status == "SKIP":
                    continue
                icon = {"PASS": "+", "WARN": "~", "FAIL": "!"}[r.status]
                print(f"  [{icon}] {r.check_id}. {r.name}: {r.status}", end="")
                if r.message:
                    print(f" -- {r.message}", end="")
                print()
                if args.verbose and r.details:
                    for d in r.details:
                        print(f"      {d}")

    print()
    agents_clean = sum(
        1 for results in all_results.values() if all(r.status in ("PASS", "SKIP") for r in results)
    )
    agents_warn = sum(
        1
        for results in all_results.values()
        if any(r.status == "WARN" for r in results) and not any(r.status == "FAIL" for r in results)
    )
    agents_fail = sum(
        1 for results in all_results.values() if any(r.status == "FAIL" for r in results)
    )
    print(
        f"SUMMARY: {len(all_results)} agents -- "
        f"{agents_clean} clean, {agents_warn} warnings, {agents_fail} failures"
    )

    sys.exit(1 if total_fail > 0 else 0)


if __name__ == "__main__":
    main()
