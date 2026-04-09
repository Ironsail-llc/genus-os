#!/usr/bin/env python3
"""Deterministic GitHub data collection for the devops report pipeline.

Collects PR stats, review stats, and open PRs for configured repos.
Writes structured JSON to /tmp/devops_github_data.json.

Called by the devops-report-pipeline workflow as a tool step (no LLM needed).

Configure repos via DEVOPS_GITHUB_REPOS env var (comma-separated owner/repo).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.github_api import (
    _github_list_prs,
    _github_pr_stats,
    _github_review_stats,
)

OUTPUT_PATH = Path("/tmp/devops_github_data.json")
_repos_env = os.environ.get("DEVOPS_GITHUB_REPOS", "")
REPOS: list[str] = [r.strip() for r in _repos_env.split(",") if r.strip()]
CTX = ToolContext(agent_id="devops-manager")


async def _collect_repo(repo: str, now: datetime) -> tuple[str, dict, list, list]:
    """Collect data for a single repo. Returns (short_name, repo_data, stale_prs, errors)."""
    short = repo.split("/")[-1]
    repo_data: dict = {}
    stale: list = []
    errors: list = []

    result = await _github_pr_stats({"repo": repo, "days": 30}, CTX)
    if "error" in result:
        errors.append(f"{short}/pr_stats: {result['error']}")
    else:
        repo_data["pr_stats"] = {
            "merged_count": result.get("merged_count", 0),
            "avg_cycle_time_hours": result.get("avg_cycle_time_hours", 0),
            "median_cycle_time_hours": result.get("median_cycle_time_hours", 0),
            "authors": result.get("authors", {}),
        }

    result = await _github_review_stats({"repo": repo, "days": 30}, CTX)
    if "error" in result:
        errors.append(f"{short}/review_stats: {result['error']}")
    else:
        reviewers = [
            {
                "reviewer": r["reviewer"],
                "reviews_given": r["reviews_given"],
                "approvals": r["approvals"],
                "changes_requested": r["changes_requested"],
                "avg_turnaround_hours": r.get("avg_turnaround_hours"),
            }
            for r in result.get("reviewers", [])
            if not r["reviewer"].endswith("[bot]")
        ]
        repo_data["review_stats"] = {
            "prs_analyzed": result.get("prs_analyzed", 0),
            "reviewers": reviewers,
        }

    result = await _github_list_prs(
        {"repo": repo, "state": "open", "per_page": 50, "max_pages": 1}, CTX
    )
    if "error" not in result:
        for pr in result.get("pull_requests", []):
            if pr.get("draft"):
                continue
            created = datetime.fromisoformat(pr["created_at"])
            age_days = (now - created).total_seconds() / 86400
            if age_days > 7:
                stale.append(
                    {
                        "repo": short,
                        "number": pr["number"],
                        "title": pr["title"][:60],
                        "author": pr["author"],
                        "age_days": round(age_days, 1),
                        "is_bot": pr["author"].endswith("[bot]")
                        or pr["author"] == "dependabot[bot]",
                    }
                )

    return short, repo_data, stale, errors


async def collect() -> dict:
    data: dict = {"repos": {}, "totals": {"merged": 0, "reviews": 0}, "stale_prs": [], "errors": []}
    now = datetime.now(UTC)

    # Run all repos concurrently
    results = await asyncio.gather(*[_collect_repo(repo, now) for repo in REPOS])

    for short, repo_data, stale, errors in results:
        if repo_data:
            data["repos"][short] = repo_data
            data["totals"]["merged"] += repo_data.get("pr_stats", {}).get("merged_count", 0)
            data["totals"]["reviews"] += sum(
                r["reviews_given"] for r in repo_data.get("review_stats", {}).get("reviewers", [])
            )
        data["stale_prs"].extend(stale)
        data["errors"].extend(errors)

    data["stale_prs"].sort(key=lambda p: -p["age_days"])
    return data


def main() -> int:
    if not REPOS:
        print("DEVOPS_GITHUB_REPOS not set — skipping GitHub collection.", flush=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "repos": {},
                    "totals": {"merged": 0, "reviews": 0},
                    "stale_prs": [],
                    "errors": ["DEVOPS_GITHUB_REPOS not configured"],
                }
            )
        )
        return 0
    print("Collecting GitHub data...", flush=True)
    result = asyncio.run(collect())

    OUTPUT_PATH.write_text(json.dumps(result, indent=2))
    merged = result["totals"]["merged"]
    repos = len(result["repos"])
    stale = len(result["stale_prs"])
    errors = len(result["errors"])
    print(
        f"Done: {merged} merged PRs across {repos} repos, {stale} stale PRs, {errors} errors → {OUTPUT_PATH}"
    )

    return 1 if errors and not repos else 0


if __name__ == "__main__":
    sys.exit(main())
