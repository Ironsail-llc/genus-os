#!/usr/bin/env python3
"""Validate agent manifests against the schema contract.

Thin CLI wrapper around robothor.templates.manifest_checks.

Loads docs/agents/schema.yaml as the source of truth and enforces:
  A. Schema required fields (strict -- blocks commit)
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
  M. SecretRef keys present in environment

Usage:
    python scripts/validate_agents.py                   # Check all agents
    python scripts/validate_agents.py --agent <id>      # Check one agent
    python scripts/validate_agents.py --verbose          # Show details
    python scripts/validate_agents.py --json             # JSON output
"""

import argparse
import json
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

sys.path.insert(0, str(REPO_ROOT))


def _import_checks():
    """Lazy import to satisfy E402 (module-level import not at top of file)."""
    from robothor.templates.manifest_checks import load_schema, validate_agent

    return load_schema, validate_agent


def load_manifests(agent_id: str | None = None) -> dict:
    """Load YAML manifests from docs/agents/.

    Only tracked manifests are validated — instance-local (gitignored)
    manifests are skipped so the platform validator never fails on agents
    that live only in a single operator's fleet.
    """
    import subprocess

    try:
        tracked = subprocess.check_output(
            ["git", "ls-files", "docs/agents/*.yaml"],
            cwd=REPO_ROOT,
            text=True,
        )
        tracked_paths = {REPO_ROOT / p for p in tracked.splitlines() if p.strip()}
    except (subprocess.SubprocessError, FileNotFoundError):
        # Fall back to globbing everything if git isn't available (tarball install).
        tracked_paths = set(MANIFEST_DIR.glob("*.yaml"))

    manifests = {}
    for f in sorted(MANIFEST_DIR.glob("*.yaml")):
        if f not in tracked_paths:
            continue
        with f.open() as fh:
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
        from robothor.engine.tools import ToolRegistry

        registry = ToolRegistry()
        return set(registry._schemas.keys())
    except Exception as e:
        print(f"WARNING: Cannot load ToolRegistry: {e}", file=sys.stderr)
        return set()


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
    parser.add_argument(
        "--chain",
        action="store_true",
        help="Run chain validation checks M-R in addition to A-L",
    )
    args = parser.parse_args()

    load_schema, validate_agent = _import_checks()

    # Load schema
    schema, required_fields, departments = load_schema(SCHEMA_PATH)
    if schema:
        print(f"Schema: {len(required_fields)} required fields loaded from schema.yaml")
    else:
        print("Schema: schema.yaml not found, using minimal required fields")

    # Load all TRACKED manifests. Agent manifests are instance config and
    # mostly gitignored, so a clean platform checkout yields zero. That's
    # fine — the platform ships no enforced fleet.
    all_manifests = load_manifests()
    if not all_manifests:
        print(
            "OK: No tracked manifests found — agent manifests are instance config, not platform code."
        )
        sys.exit(0)

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

    # Optionally import chain validator
    chain_validate = None
    if args.chain:
        try:
            from robothor.templates.chain_validator import validate_chain

            chain_validate = validate_chain
        except ImportError as e:
            print(f"WARNING: Cannot import chain_validator: {e}", file=sys.stderr)

    # Run validation
    all_results = {}
    total_pass = 0
    total_warn = 0
    total_fail = 0
    total_skip = 0

    for agent_id, manifest in sorted(target.items()):
        results = validate_agent(
            manifest,
            all_manifests,
            registered_tools,
            repo_root=REPO_ROOT,
            ci=args.ci,
        )

        # Append chain checks M-R if requested
        if chain_validate:
            chain_results = chain_validate(manifest, all_manifests, repo_root=REPO_ROOT)
            results.extend(chain_results)

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
