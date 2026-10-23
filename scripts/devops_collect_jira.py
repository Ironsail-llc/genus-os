#!/usr/bin/env python3
"""Collect one report week of JIRA facts for the devops report pipeline.

Writes ``$DEVOPS_RUNS_DIR/<week-start>/jira.json``: per project, every issue
resolved in the week (assignee + issue type, fully paginated — no 100-ticket
cap), issues currently in progress, the open-backlog size, and the oldest
stale tickets. Same week as the GitHub collector: the previous ISO week in
America/New_York, or ``--week-start YYYY-MM-DD``.

Delivered = resolved in the week, or moved into one of
``DEVOPS_JIRA_DONE_STATUSES`` (comma-separated, e.g. a "Deployed" column on a
board that leaves shipped issues unresolved) in the week.

Projects: ``DEVOPS_JIRA_PROJECTS`` (comma-separated), else
``brain/memory/devops-config.json`` → ``jira.active_projects``.

JQL bare dates are evaluated in the JIRA account's profile timezone; set it to
America/New_York for exact alignment with the GitHub window.

Exit status 1 when any query failed; the JSON is written regardless, with the
failed query named in ``errors`` and the project's ``unavailable`` map.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.devops_periods import report_window, window_block
from robothor.engine.reports.devops_team import env_list, runs_dir, week_run_dir
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.jira import _jira_search

CONFIG_PATH = Path(__file__).resolve().parent.parent / "brain" / "memory" / "devops-config.json"
CTX = ToolContext(agent_id="devops-manager")
PAGE_SIZE = 100
#: Safety stop for a runaway query (10,000 issues). Hitting it is reported.
MAX_PAGES = 100
STALE_LIST = 10


def load_projects() -> list[str]:
    env_val = env_list("DEVOPS_JIRA_PROJECTS")
    if env_val:
        return env_val
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        return list(cfg.get("jira", {}).get("active_projects", []))
    return []


async def _search_all(
    search, jql: str, *, limit: int | None = None
) -> tuple[list[dict], str | None]:
    """Every issue for ``jql`` (or the first ``limit``). Returns (issues, error)."""
    issues: list[dict] = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        args: dict[str, Any] = {"jql": jql, "max_results": min(PAGE_SIZE, limit or PAGE_SIZE)}
        if token:
            args["next_page_token"] = token
        result = await search(args, CTX)
        if "error" in result:
            return issues, str(result["error"])
        issues.extend(result.get("issues") or [])
        if limit and len(issues) >= limit:
            return issues[:limit], None
        token = result.get("next_page_token")
        if result.get("is_last", True) or not token:
            return issues, None
    return issues, f"stopped after {MAX_PAGES} pages ({len(issues)} issues); result truncated"


def _slim(i: dict) -> dict:
    return {
        "key": i.get("key", ""),
        "assignee": i.get("assignee") or "Unassigned",
        "type": i.get("issue_type") or "Other",
        "status": i.get("status", ""),
        "summary": (i.get("summary") or "")[:100],
    }


def _quoted(names: list[str]) -> str:
    return ", ".join(f'"{n}"' for n in names)


async def collect_jira(
    projects: list[str],
    start: str,
    end: str,
    search=None,
    *,
    done_statuses: list[str] | None = None,
) -> dict:
    """``done_statuses``: statuses that mean delivered even while the issue is
    unresolved (a board whose last column is e.g. "Deployed"). Entering one in
    the week counts as delivered, and such issues are not "in progress"."""
    search = search or _jira_search
    data: dict[str, Any] = {"projects": {}, "errors": []}
    done = _quoted(done_statuses) if done_statuses else ""
    delivered = f'resolved >= "{start}" AND resolved < "{end}"'
    not_done = ""
    if done:
        delivered = (
            f'(({delivered} AND NOT status WAS IN ({done}) BEFORE "{start}") '
            f'OR status CHANGED TO ({done}) DURING ("{start}", "{end}"))'
        )
        not_done = f" AND status NOT IN ({done})"

    for proj in projects:
        pd: dict[str, Any] = {
            "resolved": [],
            "in_progress": [],
            "open_count": None,
            "stale": [],
            "unavailable": {},
        }
        queries = {
            "resolved": (
                f'project = "{proj}" AND {delivered} ORDER BY updated DESC',
                None,
            ),
            "in_progress": (
                f"project = \"{proj}\" AND statusCategory = 'In Progress'{not_done}",
                None,
            ),
            "open": (f'project = "{proj}" AND resolution = Unresolved{not_done}', None),
            "stale": (
                f'project = "{proj}" AND resolution = Unresolved{not_done} AND updated <= -14d '
                "ORDER BY updated ASC",
                STALE_LIST,
            ),
        }
        for name, (jql, limit) in queries.items():
            issues, err = await _search_all(search, jql, limit=limit)
            if err:
                data["errors"].append({"project": proj, "query": name, "message": err})
                pd["unavailable"][name] = err
                if name == "open":
                    continue
            if name == "open":
                pd["open_count"] = len(issues)
            else:
                pd[name] = [_slim(i) for i in issues]
        data["projects"][proj] = pd

    data["complete"] = not data["errors"]
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    out_path = week_run_dir(period["week_start"], args.runs_dir or runs_dir()) / "jira.json"

    projects = load_projects()
    if projects:
        data = asyncio.run(
            collect_jira(
                projects,
                period["week_start"],
                end.strftime("%Y-%m-%d"),
                _jira_search,
                done_statuses=env_list("DEVOPS_JIRA_DONE_STATUSES"),
            )
        )
    else:
        data = {
            "projects": {},
            "errors": [
                {"project": "*", "query": "config", "message": "no projects (DEVOPS_JIRA_PROJECTS)"}
            ],
            "complete": False,
        }
    data["period"] = period
    data["collected_at"] = datetime.now(UTC).isoformat()
    out_path.write_text(json.dumps(data, indent=2))

    resolved = sum(len(p["resolved"]) for p in data["projects"].values())
    print(f"{resolved} resolved tickets, {len(data['errors'])} errors -> {out_path}")
    for e in data["errors"]:
        print(f"  ERROR {e['project']} {e['query']}: {e['message']}", file=sys.stderr)
    return 0 if data["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
