#!/usr/bin/env python3
"""Deterministic JIRA data collection for the devops report pipeline.

Collects resolved tickets, open backlog, stale tickets, and in-progress work
across configured JIRA projects. Writes structured JSON to /tmp/devops_jira_data.json.

Called by the devops-report-pipeline workflow as a tool step (no LLM needed).

Configuration (in priority order):
  1. DEVOPS_JIRA_PROJECTS env var (comma-separated — for CI/tests)
  2. brain/memory/devops-config.json → jira.active_projects (canonical for prod)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.jira import _jira_search

OUTPUT_PATH = Path("/tmp/devops_jira_data.json")
CONFIG_PATH = Path(__file__).resolve().parent.parent / "brain" / "memory" / "devops-config.json"


def _load_projects() -> list[str]:
    env_val = os.environ.get("DEVOPS_JIRA_PROJECTS", "")
    if env_val:
        return [p.strip() for p in env_val.split(",") if p.strip()]
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        return list(cfg.get("jira", {}).get("active_projects", []))
    return []


PROJECTS: list[str] = _load_projects()
CTX = ToolContext(agent_id="devops-manager")


def _week_windows(now: datetime) -> tuple[str, str, str]:
    """Calculate Monday-aligned JQL date strings.

    Returns (current_week_start, last_week_start, last_week_end) as YYYY-MM-DD strings.
    """
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_monday = now.weekday()
    current_week_start = today_midnight - timedelta(days=days_since_monday)
    last_week_start = current_week_start - timedelta(days=7)
    return (
        current_week_start.strftime("%Y-%m-%d"),
        last_week_start.strftime("%Y-%m-%d"),
        current_week_start.strftime("%Y-%m-%d"),
    )


def _aggregate_issues(issues: list[dict]) -> dict:
    """Aggregate issues by assignee and type."""
    by_assignee: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for i in issues:
        a = i.get("assignee", "Unassigned")
        by_assignee[a] = by_assignee.get(a, 0) + 1
        t = i.get("issue_type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return {"count": len(issues), "by_assignee": by_assignee, "by_type": by_type}


async def collect() -> dict:
    data: dict = {"projects": {}, "totals": {"resolved": 0, "stale": 0}, "errors": []}
    now = datetime.now(UTC)
    current_week_start, last_week_start, last_week_end = _week_windows(now)

    for proj in PROJECTS:
        proj_data: dict = {}

        # --- Current week resolved tickets ---
        result = await _jira_search(
            {
                "jql": f"project = {proj} AND resolved >= {current_week_start} ORDER BY resolved DESC",
                "max_results": 100,
            },
            CTX,
        )
        if "error" in result:
            data["errors"].append(f"{proj}/resolved_current_week: {result['error']}")
            continue

        proj_data["resolved_current_week"] = _aggregate_issues(result.get("issues", []))
        data["totals"]["resolved"] += proj_data["resolved_current_week"]["count"]

        # --- Last week resolved tickets ---
        result = await _jira_search(
            {
                "jql": f"project = {proj} AND resolved >= {last_week_start} AND resolved < {last_week_end} ORDER BY resolved DESC",
                "max_results": 100,
            },
            CTX,
        )
        if "error" in result:
            data["errors"].append(f"{proj}/resolved_last_week: {result['error']}")
        else:
            proj_data["resolved_last_week"] = _aggregate_issues(result.get("issues", []))

        # Open backlog (no date filter — correct, leave alone)
        open_result = await _jira_search(
            {"jql": f"project = {proj} AND resolution = Unresolved", "max_results": 100},
            CTX,
        )
        if "error" not in open_result:
            open_issues = open_result.get("issues", [])
            by_status: dict[str, int] = {}
            for i in open_issues:
                s = i.get("status", "?")
                by_status[s] = by_status.get(s, 0) + 1
            proj_data["open"] = {
                "count": len(open_issues),
                "is_last": open_result.get("is_last", True),
                "by_status": by_status,
            }

        # Stale tickets (open, no update in 14+ days)
        stale_result = await _jira_search(
            {
                "jql": f"project = {proj} AND resolution = Unresolved AND updated <= -14d ORDER BY updated ASC",
                "max_results": 10,
            },
            CTX,
        )
        if "error" not in stale_result:
            stale_issues = stale_result.get("issues", [])
            proj_data["stale"] = [
                {
                    "key": i["key"],
                    "status": i["status"],
                    "assignee": i["assignee"],
                    "summary": i["summary"][:60],
                }
                for i in stale_issues
            ]
            data["totals"]["stale"] += len(stale_issues)

        # In progress
        wip_result = await _jira_search(
            {"jql": f"project = {proj} AND statusCategory = 'In Progress'", "max_results": 20},
            CTX,
        )
        if "error" not in wip_result:
            wip_issues = wip_result.get("issues", [])
            wip_by_assignee: dict[str, int] = {}
            for i in wip_issues:
                a = i.get("assignee", "Unassigned")
                wip_by_assignee[a] = wip_by_assignee.get(a, 0) + 1
            proj_data["in_progress"] = {
                "count": len(wip_issues),
                "by_assignee": wip_by_assignee,
            }

        if proj_data:
            data["projects"][proj] = proj_data

    return data


def main() -> int:
    if not PROJECTS:
        print(
            f"No JIRA projects configured. Set DEVOPS_JIRA_PROJECTS env var "
            f"or populate jira.active_projects in {CONFIG_PATH}.",
            flush=True,
        )
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "projects": {},
                    "totals": {"resolved": 0, "stale": 0},
                    "errors": [
                        f"No JIRA projects configured (env DEVOPS_JIRA_PROJECTS empty "
                        f"and {CONFIG_PATH} missing or has no jira.active_projects)."
                    ],
                }
            )
        )
        return 0
    print("Collecting JIRA data...", flush=True)
    result = asyncio.run(collect())

    OUTPUT_PATH.write_text(json.dumps(result, indent=2))
    total = result["totals"]["resolved"]
    projects = len(result["projects"])
    errors = len(result["errors"])
    print(
        f"Done: {total} resolved tickets across {projects} projects, {errors} errors → {OUTPUT_PATH}"
    )

    return 1 if errors and not projects else 0


if __name__ == "__main__":
    sys.exit(main())
