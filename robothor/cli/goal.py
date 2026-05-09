"""CLI commands for the active long-running session goal.

Backed by ``robothor.engine.session_goal`` (DAL-backed, evidence is typed).
Workspace is resolved via ``ROBOTHOR_WORKSPACE`` env var with a sane default
— there is no per-invocation ``--workspace`` flag.
"""

from __future__ import annotations

import json
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.session_goal import (
    add_evidence,
    complete_goal,
    create_active_goal,
    get_active_goal,
    regenerate_goal_md_cache,
    summarize_goal,
)


def _tenant(args: Any) -> str:
    return getattr(args, "tenant", None) or DEFAULT_TENANT


def _agent(args: Any) -> str:
    return getattr(args, "agent", None) or ""


def _print_goal(goal: Any, *, json_output: bool = False) -> None:
    data = summarize_goal(goal)
    if json_output:
        print(json.dumps(data, indent=2, sort_keys=True))
        return

    print(f"{data['id']} [{data['status']}]")
    print(data["objective"])
    print()
    print("Success criteria:")
    for criterion in data["success_criteria"]:
        print(f"- {criterion}")
    print(f"\nEvidence: {data['valid_evidence_count']}/{data['evidence_count']} validated")
    missing = data["missing_completion_requirements"]
    if missing:
        print("Missing before completion:")
        for item in missing:
            print(f"- {item}")
    else:
        print("Ready for completion.")


def cmd_goal(args: Any) -> int:
    """Dispatch goal subcommands."""
    command = getattr(args, "goal_command", None)
    tenant_id = _tenant(args)
    agent_id = _agent(args)

    if command == "set":
        try:
            goal = create_active_goal(
                tenant_id=tenant_id,
                objective=args.objective,
                criteria=args.criteria or None,
                agent_id=agent_id,
            )
        except ValueError as exc:
            print(str(exc))
            return 1
        regenerate_goal_md_cache(tenant_id=tenant_id, agent_id=agent_id)
        _print_goal(goal, json_output=getattr(args, "json_output", False))
        return 0

    if command == "status":
        goal = get_active_goal(tenant_id=tenant_id, agent_id=agent_id)
        if goal is None:
            if getattr(args, "json_output", False):
                print(json.dumps({"status": "none"}))
            else:
                print("No active goal.")
            return 1
        _print_goal(goal, json_output=getattr(args, "json_output", False))
        return 0

    if command == "evidence":
        try:
            goal = add_evidence(
                tenant_id=tenant_id,
                kind=args.kind,
                summary=args.summary,
                reference=args.reference or "",
                agent_id=agent_id,
            )
        except ValueError as exc:
            print(str(exc))
            return 1
        regenerate_goal_md_cache(tenant_id=tenant_id, agent_id=agent_id)
        _print_goal(goal, json_output=getattr(args, "json_output", False))
        return 0

    if command == "complete":
        try:
            goal = complete_goal(
                tenant_id=tenant_id,
                note=args.note,
                agent_id=agent_id,
            )
        except ValueError as exc:
            print(str(exc))
            return 1
        regenerate_goal_md_cache(tenant_id=tenant_id, agent_id=agent_id)
        _print_goal(goal, json_output=getattr(args, "json_output", False))
        return 0

    print("Missing goal subcommand. Use: set, status, evidence, complete.")
    return 1
