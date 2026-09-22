"""Factual goal prose from a trusted, complete store.get snapshot.

This renderer does not choose goals, authorize controls, or finish an agent run.
Native chat supplies authorized snapshots through report_pursuit_goal.
"""

from datetime import UTC, datetime
from typing import Any


def _label(value: str, limit: int = 160) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _task_facts(tasks: list[dict[str, Any]]) -> list[str]:
    parts = []
    done = sum(task["status"] == "DONE" for task in tasks)
    canceled = sum(task["status"] == "CANCELED" for task in tasks)
    remaining = [task for task in tasks if task["status"] in {"TODO", "IN_PROGRESS", "REVIEW"}]
    unknown = len(tasks) - done - canceled - len(remaining)
    if tasks:
        parts.append(f"{done} of {len(tasks)} linked tasks are marked done.")
        if remaining:
            titles = [f"“{_label(task.get('title') or 'Untitled task')}”" for task in remaining[:3]]
            extra = f", and {len(remaining) - 3} more" if len(remaining) > 3 else ""
            parts.append("Still open: " + ", ".join(titles) + extra + ".")
        if canceled:
            parts.append(
                f"{canceled} linked {'task is' if canceled == 1 else 'tasks are'} canceled."
            )
    else:
        parts.append("No tasks are linked to this goal.")
    if unknown:
        parts.append(
            f"{unknown} linked {'task has an' if unknown == 1 else 'tasks have'} unrecognized status."
        )
    return parts


def render_goal_progress(goal: dict[str, Any], *, execution_enabled: bool) -> str:
    status = goal["status"]
    label = {"review": "awaiting review", "running": "in progress"}.get(status, status)
    pending = (goal.get("action_evidence") or {}).get("pending", 0)
    if pending and status == "complete":
        label = "marked complete, but its action outcomes are not fully verified"
    parts = [f"“{_label(goal['objective'])}” is {label}."]
    if pending:
        parts.append(
            f"{pending} recorded action(s) in this goal or its children still need verification."
        )
    # Missing task/child reads must not become "no unfinished work".
    parts.extend(_task_facts(goal["tasks"]))
    children = [
        child for child in goal["children"] if child["status"] not in {"complete", "canceled"}
    ]
    if children:
        titles = [f"“{_label(child['objective'])}” ({child['status']})" for child in children[:3]]
        extra = f", and {len(children) - 3} more" if len(children) > 3 else ""
        parts.append("Child work remains unfinished: " + ", ".join(titles) + extra + ".")
    if goal["mode"] == "ongoing":
        parts.append("This is an ongoing goal; completed tasks do not finish it.")
    elif status != "complete":
        parts.append("The goal is not complete.")
    if not goal["evidence"]:
        parts.append("No current criterion evidence has been recorded.")
    elif status != "complete":
        parts.append("Evidence has been recorded; that alone does not establish completion.")
    if goal.get("blocker"):
        parts.append("Blocker: " + _label(goal["blocker"]) + ".")
    if goal.get("recovery_required") or any(
        child.get("recovery_required") for child in goal["children"]
    ):
        parts.append("The results of earlier actions still need verification.")
    if status not in {"complete", "canceled"}:
        if not execution_enabled:
            parts.append("Automatic pursuit is disabled, so scheduled reviews will not run.")
        elif status in {"paused", "blocked", "review"}:
            parts.append("Scheduled reviews will not run while the goal is " + label + ".")
        elif status in {"waiting", "queued"}:
            review_at = datetime.fromisoformat(goal["ready_at"]).astimezone(UTC)
            parts.append(
                f"A review is registered for {review_at:%Y-%m-%d %H:%M UTC}; "
                "execution still depends on scheduling and available budget."
            )
    return " ".join(parts)
