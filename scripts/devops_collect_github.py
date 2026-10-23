#!/usr/bin/env python3
"""Collect one report week of GitHub facts for the devops report pipeline.

Writes ``$DEVOPS_RUNS_DIR/<week-start>/github.json`` (default runs dir:
``$ROBOTHOR_WORKSPACE/local/devops/runs``). The week is the previous ISO week
in America/New_York unless ``--week-start YYYY-MM-DD`` names another.

Repos: ``DEVOPS_GITHUB_REPOS`` (comma-separated owner/repo), else
``brain/memory/devops-config.json`` → ``github.org`` + ``github.active_repos``.

Exit status: 0 when every repo and endpoint answered; 1 when anything failed
after retries. The JSON is written either way, with ``errors`` and each
repo's ``unavailable`` map, so the report can render "incomplete" instead of
reporting a failed fetch as zero activity.

See robothor/engine/reports/devops_github.py for what is collected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.engine.reports.devops_github import collect_github
from robothor.engine.reports.devops_periods import report_window, window_block
from robothor.engine.reports.devops_team import env_list, runs_dir, week_run_dir

CONFIG_PATH = Path(__file__).resolve().parent.parent / "brain" / "memory" / "devops-config.json"


def load_repos() -> list[str]:
    env_val = env_list("DEVOPS_GITHUB_REPOS")
    if env_val:
        return env_val
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text()).get("github", {})
        org = cfg.get("org", "")
        active = cfg.get("active_repos", [])
        if org and active:
            return [f"{org}/{repo}" for repo in active]
    return []


async def _collect(repos: list[str], start: datetime, end: datetime) -> dict:
    import httpx

    from robothor.engine.tools.handlers.github_api import _get_token, _headers
    from robothor.secrets import get_secret

    # A token of its own keeps the report off a quota other agents (the PR
    # reviewer) can exhaust; falls back to the shared GitHub credential.
    token = get_secret("DEVOPS_GITHUB_TOKEN") or _get_token()
    if not token:
        return {
            "repos": {},
            "errors": [
                {"repo": "*", "endpoint": "auth", "status": None, "message": "GITHUB_TOKEN not set"}
            ],
            "complete": False,
        }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        return await collect_github(
            repos,
            start,
            end,
            client=client,
            headers=_headers(token),
            max_rate_wait=float(os.environ.get("DEVOPS_GITHUB_MAX_RATE_WAIT", "600")),
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-start", help="YYYY-MM-DD (any day of the week); default last week")
    ap.add_argument("--runs-dir", type=Path, help="override $DEVOPS_RUNS_DIR")
    args = ap.parse_args(argv)

    start, end = report_window(week_start=args.week_start)
    period = window_block(start, end)
    out_path = week_run_dir(period["week_start"], args.runs_dir or runs_dir()) / "github.json"

    repos = load_repos()
    if repos:
        print(f"Collecting GitHub for {len(repos)} repos, {period['report_label']}...", flush=True)
        data = asyncio.run(_collect(repos, start, end))
    else:
        data = {
            "repos": {},
            "errors": [
                {
                    "repo": "*",
                    "endpoint": "config",
                    "status": None,
                    "message": "no repos configured (DEVOPS_GITHUB_REPOS)",
                }
            ],
            "complete": False,
        }
    data["period"] = period
    data["collected_at"] = datetime.now(UTC).isoformat()
    out_path.write_text(json.dumps(data, indent=2))

    merged = sum(len(r["merged_prs"]) for r in data["repos"].values())
    reviews = sum(len(r["reviews"]) for r in data["repos"].values())
    print(f"{merged} merged PRs, {reviews} reviews, {len(data['errors'])} errors -> {out_path}")
    for e in data["errors"]:
        print(f"  ERROR {e['repo']} {e['endpoint']} {e['status']}: {e['message']}", file=sys.stderr)
    return 0 if data["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
