"""CLI surface for persistent operator goals."""

from __future__ import annotations

import json


def add_parser(subparsers):
    parser = subparsers.add_parser("goals", help="Manage durable short- and long-term goal pursuit")
    parser.add_argument("--tenant")
    commands = parser.add_subparsers(dest="goals_command", required=True)
    commands.add_parser("list")
    for name in ("enable", "disable"):
        commands.add_parser(name)
    create = commands.add_parser("create")
    create.add_argument("objective")
    create.add_argument("--criterion", action="append", required=True)
    create.add_argument("--kind", choices=["short", "long"], default="short")
    create.add_argument("--mode", choices=["finite", "ongoing"], default="finite")
    create.add_argument("--parent-goal-id")
    create.add_argument("--token-budget", type=int)
    create.add_argument("--review-seconds", type=int, default=86400)
    create.add_argument("--human-review", action="store_true")
    commands.add_parser("adopt").add_argument("legacy_task_id")
    for name in ("get", "pause", "resume", "cancel", "approve"):
        command = commands.add_parser(name)
        command.add_argument("goal_id")
        if name == "resume":
            command.add_argument("--token-budget", type=int)
    update = commands.add_parser("update", help="Apply a versioned GoalUpdate JSON object")
    update.add_argument("goal_id")
    update.add_argument("--file", required=True, help="JSON file, or - for stdin")


def cmd_goals(args) -> int:
    import sys
    from pathlib import Path

    from robothor.constants import DEFAULT_TENANT
    from robothor.goals import store
    from robothor.goals.model import CreateGoal, GoalUpdate

    tenant = args.tenant or DEFAULT_TENANT
    actor = "operator:cli"
    try:
        command = args.goals_command
        if command == "list":
            result = {"goals": store.list_goals(tenant), "enabled": store.enabled(tenant)}
        elif command in {"enable", "disable"}:
            store.set_enabled(tenant, command == "enable", actor)
            result = {"enabled": command == "enable"}
        elif command == "create":
            result = store.create(
                tenant,
                CreateGoal(
                    objective=args.objective,
                    success_criteria=args.criterion,
                    kind=args.kind,
                    mode=args.mode,
                    parent_goal_id=args.parent_goal_id,
                    token_budget=args.token_budget,
                    review_seconds=args.review_seconds,
                    human_review=args.human_review,
                ),
                actor,
            )
        elif command == "adopt":
            result = store.adopt(tenant, args.legacy_task_id, actor)
        elif command == "get":
            result = store.get(tenant, args.goal_id)
        else:
            if command == "update":
                raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
                change = GoalUpdate.model_validate_json(raw)
            else:
                goal = store.get(tenant, args.goal_id)
                change = GoalUpdate(
                    action=command,
                    version=goal["version"],
                    token_budget=getattr(args, "token_budget", None),
                )
            result = store.update(tenant, args.goal_id, change, actor, operator=True)
        print(json.dumps(result, default=str, indent=2))
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
