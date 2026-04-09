#!/usr/bin/env python3
"""Deterministic JIRA data collection for the devops report pipeline.

Collects resolved tickets, open backlog, stale tickets, and in-progress work
across configured JIRA projects. Writes structured JSON to /tmp/devops_jira_data.json.

Called by the devops-report-pipeline workflow as a tool step (no LLM needed).

Configure projects via DEVOPS_JIRA_PROJECTS env var (comma-separated project keys).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.jira import _jira_search

OUTPUT_PATH = Path("/tmp/devops_jira_data.json")
_projects_env = os.environ.get("DEVOPS_JIRA_PROJECTS", "")
PROJECTS: list[str] = [p.strip() for p in _projects_env.split(",") if p.strip()]
CTX = ToolContext(agent_id="devops-manager")


async def collect() -> dict:
    data: dict = {"projects": {}, "totals": {"resolved": 0, "stale": 0}, "errors": []}

    for proj in PROJECTS:
        proj_data: dict = {}

        # Resolved tickets (30 days)
        result = await _jira_search(
            {
                "jql": f"project = {proj} AND resolved >= -30d ORDER BY resolved DESC",
                "max_results": 100,
            },
            CTX,
        )
        if "error" in result:
            data["errors"].append(f"{proj}/resolved: {result['error']}")
            continue

        issues = result.get("issues", [])
        if not issues:
            continue

        by_assignee: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for i in issues:
            a = i.get("assignee", "Unassigned")
            by_assignee[a] = by_assignee.get(a, 0) + 1
            t = i.get("issue_type", "?")
            by_type[t] = by_type.get(t, 0) + 1

        proj_data["resolved"] = {
            "count": len(issues),
            "by_assignee": by_assignee,
            "by_type": by_type,
        }
        data["totals"]["resolved"] += len(issues)

        # Open backlog
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
        print("DEVOPS_JIRA_PROJECTS not set — skipping JIRA collection.", flush=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "projects": {},
                    "totals": {"resolved": 0, "stale": 0},
                    "errors": ["DEVOPS_JIRA_PROJECTS not configured"],
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
