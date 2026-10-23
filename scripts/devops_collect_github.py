#!/usr/bin/env python3
"""Deterministic GitHub data collection for the devops report pipeline.

Collects PR stats, review stats, and open PRs for configured repos.
Writes structured JSON to /tmp/devops_github_data.json.

Called by the devops-report-pipeline workflow as a tool step (no LLM needed).

Configuration (in priority order):
  1. DEVOPS_GITHUB_REPOS env var (comma-separated owner/repo — for CI/tests)
  2. brain/memory/devops-config.json → github.org + github.active_repos (canonical for prod)

Rate-limit note: the PR-stats handler reads GitHub's /search/issues endpoint, which
enforces a *secondary* rate limit triggered by short bursts of concurrent requests and
answers 403. Polling every repo at once made most repos fail; a failed repo simply
contributes no `pr_stats_*` keys, so the report rendered "0 merged PRs" instead of an
error. Two guards prevent that: repo polling is capped by a semaphore, and every
handler call is retried on a transient error with backoff. The retry is itself capped
by a global budget so this step cannot overrun its 120s exec timeout when GitHub is
genuinely unavailable.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from robothor.engine.reports.devops_periods import ET, period_block, week_windows_github
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.github_api import (
    _github_list_prs,
    _github_pr_stats,
    _github_review_stats,
)

OUTPUT_PATH = Path("/tmp/devops_github_data.json")
CONFIG_PATH = Path(__file__).resolve().parent.parent / "brain" / "memory" / "devops-config.json"

# Cap on repos polled at once. Only the /search/issues-backed calls (pr_stats) trip
# GitHub's secondary rate limit, and it penalises CONCURRENCY itself ("make requests
# serially"), not just volume: at 2 concurrent repos, 2 of 6 repos still 403'd after
# full retries (merged=10, errors=2). The core-REST calls (review_stats, list_prs) never
# failed. So repo-level parallelism is kept for the core calls, and the search calls are
# serialised through _SEARCH_LOCK below. Fully serialising every repo also fixed the 403s
# (merged=36, errors=[]) but took 138s, over the 120s step budget.
MAX_CONCURRENT_REPOS = 3
# Backoff between retries of a transient GitHub failure (seconds). Kept short: the
# secondary limit clears in seconds, and a long backoff would stall the whole report.
RETRY_DELAYS_SECONDS = (2.0, 5.0)
# Total retry time allowed across the whole collection. The collect-github step aborts
# the pipeline if it exceeds 120s, so a hard cap on backoff keeps a GitHub outage from
# escalating a wrong report into a failed run.
RETRY_BUDGET_SECONDS = 30.0
# Errors that retrying will not fix (bad/expired token, missing repo).
_PERMANENT_ERROR_MARKERS = ("404", "not found", "401", "unauthorized", "bad credentials")

# Set by collect(); retries are skipped once this passes.
_retry_deadline: float = float("inf")

# Serialises the /search/issues-backed pr_stats calls across ALL repos. GitHub's secondary
# rate limit is tripped by concurrent search requests from one client, so these run one at
# a time while the core-REST calls stay parallel. Created in collect() (needs a loop).
_search_lock: "asyncio.Semaphore | None" = None


def _load_repos() -> list[str]:
    env_val = os.environ.get("DEVOPS_GITHUB_REPOS", "")
    if env_val:
        return [r.strip() for r in env_val.split(",") if r.strip()]
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text()).get("github", {})
        org = cfg.get("org", "")
        active = cfg.get("active_repos", [])
        if not org or not active:
            return []
        return [f"{org}/{repo}" for repo in active]
    return []


REPOS: list[str] = _load_repos()
CTX = ToolContext(agent_id="devops-manager")


async def _call_with_retry(call):
    """Run a GitHub handler call, retrying transient failures with backoff.

    Handlers signal failure by returning a dict with an ``error`` key rather than
    raising. Rate-limit (403/429) and timeout/5xx errors are transient, so retry them;
    permanent errors (401/404) are returned straight away, as is any error once the
    overall retry budget is spent.
    """
    result = await call()
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        if "error" not in result:
            return result
        message = str(result.get("error", "")).lower()
        if any(marker in message for marker in _PERMANENT_ERROR_MARKERS):
            return result
        if time.monotonic() >= _retry_deadline:
            return result
        print(
            f"  transient GitHub error (retry {attempt}/{len(RETRY_DELAYS_SECONDS)}): "
            f"{result['error']}",
            file=sys.stderr,
            flush=True,
        )
        await asyncio.sleep(delay)
        result = await call()
    return result


async def _collect_repo(repo: str, now: datetime) -> tuple[str, dict, list, list]:
    """Collect data for a single repo. Returns (short_name, repo_data, stale_prs, errors)."""
    short = repo.split("/")[-1]
    repo_data: dict = {}
    stale: list = []
    errors: list = []

    current_week_since, last_week_since, last_week_until = week_windows_github(now)

    # --- Current week PR stats (Monday → now) ---
    async with _search_lock:
        result = await _call_with_retry(
            lambda: _github_pr_stats({"repo": repo, "since": current_week_since}, CTX)
        )
    if "error" in result:
        errors.append(f"{short}/pr_stats_current_week: {result['error']}")
    else:
        repo_data["pr_stats_current_week"] = {
            "merged_count": result.get("merged_count", 0),
            "avg_cycle_time_hours": result.get("avg_cycle_time_hours", 0),
            "median_cycle_time_hours": result.get("median_cycle_time_hours", 0),
            "authors": result.get("authors", {}),
            "merged_by": result.get("merged_by", {}),
        }

    # --- Last week PR stats (last Monday → this Monday, exclusive) ---
    async with _search_lock:
        result = await _call_with_retry(
            lambda: _github_pr_stats(
                {
                    "repo": repo,
                    "since": last_week_since,
                    "until": last_week_until,
                },
                CTX,
            )
        )
    if "error" in result:
        errors.append(f"{short}/pr_stats_last_week: {result['error']}")
    else:
        repo_data["pr_stats_last_week"] = {
            "merged_count": result.get("merged_count", 0),
            "avg_cycle_time_hours": result.get("avg_cycle_time_hours", 0),
            "median_cycle_time_hours": result.get("median_cycle_time_hours", 0),
            "authors": result.get("authors", {}),
            "merged_by": result.get("merged_by", {}),
        }

    # --- Review stats (30-day, unchanged) ---
    result = await _call_with_retry(lambda: _github_review_stats({"repo": repo, "days": 30}, CTX))
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

    # --- Stale open PRs ---
    result = await _call_with_retry(
        lambda: _github_list_prs(
            {"repo": repo, "state": "open", "per_page": 50, "max_pages": 1}, CTX
        )
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
    global _retry_deadline, _search_lock
    _retry_deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    _search_lock = asyncio.Semaphore(1)

    now = datetime.now(ET)
    data: dict = {
        "period": period_block(now),
        "repos": {},
        "totals": {"merged": 0, "reviews": 0},
        "stale_prs": [],
        "errors": [],
    }

    # Poll repos with bounded concurrency so we do not trip GitHub's /search secondary
    # rate limit (unbounded gather was the root cause of the missing PR stats).
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REPOS)

    async def _bounded(repo: str) -> tuple[str, dict, list, list]:
        async with semaphore:
            return await _collect_repo(repo, now)

    results = await asyncio.gather(*[_bounded(repo) for repo in REPOS])

    for short, repo_data, stale, errors in results:
        if repo_data:
            data["repos"][short] = repo_data
            data["totals"]["merged"] += repo_data.get("pr_stats_current_week", {}).get(
                "merged_count", 0
            )
            data["totals"]["reviews"] += sum(
                r["reviews_given"] for r in repo_data.get("review_stats", {}).get("reviewers", [])
            )
        data["stale_prs"].extend(stale)
        data["errors"].extend(errors)

    data["stale_prs"].sort(key=lambda p: -p["age_days"])
    return data


def main() -> int:
    if not REPOS:
        print(
            f"No GitHub repos configured. Set DEVOPS_GITHUB_REPOS env var "
            f"or populate github.org + github.active_repos in {CONFIG_PATH}.",
            flush=True,
        )
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "repos": {},
                    "totals": {"merged": 0, "reviews": 0},
                    "stale_prs": [],
                    "errors": [
                        f"No GitHub repos configured (env DEVOPS_GITHUB_REPOS empty "
                        f"and {CONFIG_PATH} missing or has no github.active_repos)."
                    ],
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
