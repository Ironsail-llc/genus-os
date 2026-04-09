"""GitHub REST API tool handlers for dev team operations monitoring."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_GITHUB_API = "https://api.github.com"


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _get_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _slim_pr(pr: dict[str, Any]) -> dict[str, Any]:
    """Extract key fields from a GitHub PR."""
    user = pr.get("user") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "state": pr.get("state", ""),
        "author": user.get("login", ""),
        "created_at": pr.get("created_at", ""),
        "updated_at": pr.get("updated_at", ""),
        "merged_at": pr.get("merged_at"),
        "closed_at": pr.get("closed_at"),
        "draft": pr.get("draft", False),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "review_decision": pr.get("review_decision"),
        "merged_by": (pr.get("merged_by") or {}).get("login", ""),
        "labels": [label.get("name", "") for label in (pr.get("labels") or [])],
    }


async def _paginate(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any],
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Follow GitHub pagination (Link header) up to max_pages."""
    results: list[dict[str, Any]] = []
    next_url: str | None = url

    for _ in range(max_pages):
        if not next_url:
            break
        resp = await client.get(
            next_url, headers=headers, params=params if next_url == url else None
        )
        resp.raise_for_status()
        results.extend(resp.json())

        # Parse Link header for next page
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break

    return results


@_handler("github_list_prs")
async def _github_list_prs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List pull requests for a repository."""
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    repo = args.get("repo", "")
    if not repo:
        return {"error": "repo is required (format: owner/repo)"}

    state = args.get("state", "all")
    sort = args.get("sort", "updated")
    per_page = min(args.get("per_page", 30), 100)
    max_pages = min(args.get("max_pages", 3), 5)

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            prs = await _paginate(
                client,
                f"{_GITHUB_API}/repos/{repo}/pulls",
                _headers(token),
                {"state": state, "sort": sort, "direction": "desc", "per_page": per_page},
                max_pages=max_pages,
            )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Repository {repo} not found"}
        if e.response.status_code == 401:
            return {"error": "GitHub authentication failed — check token"}
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    return {
        "pull_requests": [_slim_pr(pr) for pr in prs],
        "count": len(prs),
        "repo": repo,
    }


@_handler("github_get_pr")
async def _github_get_pr(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get a single PR with review timeline and details."""
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    repo = args.get("repo", "")
    pr_number = args.get("pr_number")
    if not repo or not pr_number:
        return {"error": "repo and pr_number are required"}

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # Get PR details
            resp = await client.get(
                f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}",
                headers=_headers(token),
            )
            if resp.status_code == 404:
                return {"error": f"PR #{pr_number} not found in {repo}"}
            resp.raise_for_status()
            pr = resp.json()

            # Get reviews
            resp = await client.get(
                f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
                headers=_headers(token),
            )
            resp.raise_for_status()
            reviews = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    result = _slim_pr(pr)

    # Add review details
    result["reviews"] = [
        {
            "reviewer": (r.get("user") or {}).get("login", ""),
            "state": r.get("state", ""),
            "submitted_at": r.get("submitted_at", ""),
        }
        for r in reviews
    ]

    # Calculate time to first review
    if reviews and pr.get("created_at"):
        first_review_time = min(r.get("submitted_at", "") for r in reviews if r.get("submitted_at"))
        if first_review_time:
            created = datetime.fromisoformat(pr["created_at"])
            reviewed = datetime.fromisoformat(first_review_time)
            delta = reviewed - created
            result["hours_to_first_review"] = round(delta.total_seconds() / 3600, 1)

    return result


@_handler("github_pr_stats")
async def _github_pr_stats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get aggregated PR metrics for a repo over a date range."""
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    repo = args.get("repo", "")
    if not repo:
        return {"error": "repo is required (format: owner/repo)"}

    days = min(args.get("days", 30), 90)
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    since = since - timedelta(days=days)

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            # Get merged PRs
            prs = await _paginate(
                client,
                f"{_GITHUB_API}/repos/{repo}/pulls",
                _headers(token),
                {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100},
                max_pages=5,
            )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Repository {repo} not found"}
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    # Filter to merged PRs within date range (skip closed-but-not-merged)
    merged_prs = []
    for pr in prs:
        merged_at = pr.get("merged_at")
        if not merged_at:
            continue
        merged_dt = datetime.fromisoformat(merged_at)
        if merged_dt >= since.replace(tzinfo=UTC):
            merged_prs.append(pr)

    if not merged_prs:
        return {
            "repo": repo,
            "days": days,
            "merged_count": 0,
            "message": "No merged PRs in this period",
        }

    # Calculate metrics — exclude drafts from cycle time to avoid inflation
    cycle_times_hours = []
    sizes = []
    for pr in merged_prs:
        created = datetime.fromisoformat(pr["created_at"])
        merged = datetime.fromisoformat(pr["merged_at"])
        # Skip drafts from cycle time calc (they inflate averages)
        if pr.get("draft"):
            continue
        cycle_times_hours.append((merged - created).total_seconds() / 3600)
        sizes.append((pr.get("additions") or 0) + (pr.get("deletions") or 0))

    # Author breakdown
    author_counts: dict[str, int] = {}
    for pr in merged_prs:
        author = (pr.get("user") or {}).get("login", "unknown")
        author_counts[author] = author_counts.get(author, 0) + 1

    # Merged-by breakdown (who actually clicked merge)
    merged_by_counts: dict[str, int] = {}
    for pr in merged_prs:
        merger = (pr.get("merged_by") or {}).get("login", "unknown")
        merged_by_counts[merger] = merged_by_counts.get(merger, 0) + 1

    return {
        "repo": repo,
        "days": days,
        "merged_count": len(merged_prs),
        "avg_cycle_time_hours": round(sum(cycle_times_hours) / len(cycle_times_hours), 1)
        if cycle_times_hours
        else 0,
        "median_cycle_time_hours": round(sorted(cycle_times_hours)[len(cycle_times_hours) // 2], 1)
        if cycle_times_hours
        else 0,
        "avg_size_lines": round(sum(sizes) / len(sizes)) if sizes else 0,
        "authors": author_counts,
        "merged_by": merged_by_counts,
    }


@_handler("github_commit_activity")
async def _github_commit_activity(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get commit frequency by contributor."""
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    repo = args.get("repo", "")
    if not repo:
        return {"error": "repo is required (format: owner/repo)"}

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            # GitHub stats endpoint may return 202 on first call (computing)
            for _attempt in range(3):
                resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/stats/contributors",
                    headers=_headers(token),
                )
                if resp.status_code == 202:
                    await asyncio.sleep(2)
                    continue
                if resp.status_code == 404:
                    return {"error": f"Repository {repo} not found"}
                resp.raise_for_status()
                break
            else:
                return {"error": "GitHub is still computing stats — try again shortly"}

            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    if not isinstance(data, list):
        return {"repo": repo, "contributors": [], "message": "No contributor data available"}

    weeks_to_show = min(args.get("weeks", 12), 52)

    contributors = []
    for entry in data:
        author = (entry.get("author") or {}).get("login", "unknown")
        total = entry.get("total", 0)
        recent_weeks = (entry.get("weeks") or [])[-weeks_to_show:]
        recent_commits = sum(w.get("c", 0) for w in recent_weeks)
        recent_additions = sum(w.get("a", 0) for w in recent_weeks)
        recent_deletions = sum(w.get("d", 0) for w in recent_weeks)

        if recent_commits > 0:
            contributors.append(
                {
                    "author": author,
                    "total_commits": total,
                    "recent_commits": recent_commits,
                    "recent_additions": recent_additions,
                    "recent_deletions": recent_deletions,
                }
            )

    contributors.sort(key=lambda c: c["recent_commits"], reverse=True)

    return {
        "repo": repo,
        "weeks": weeks_to_show,
        "contributors": contributors,
    }


@_handler("github_review_stats")
async def _github_review_stats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get code review participation stats."""
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    repo = args.get("repo", "")
    if not repo:
        return {"error": "repo is required (format: owner/repo)"}

    days = min(args.get("days", 30), 90)
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    since = since - timedelta(days=days)

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            # Get recent closed PRs
            prs = await _paginate(
                client,
                f"{_GITHUB_API}/repos/{repo}/pulls",
                _headers(token),
                {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 50},
                max_pages=3,
            )

            # Filter to date range
            recent_prs = []
            for pr in prs:
                updated = datetime.fromisoformat(pr["updated_at"])
                if updated >= since.replace(tzinfo=UTC):
                    recent_prs.append(pr)

            # Gather reviews for each PR
            reviewer_stats: dict[str, dict[str, Any]] = {}

            for pr in recent_prs[:50]:  # Cap to avoid rate limits
                resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/pulls/{pr['number']}/reviews",
                    headers=_headers(token),
                )
                if resp.status_code != 200:
                    continue
                reviews = resp.json()

                pr_created = datetime.fromisoformat(pr["created_at"])

                for review in reviews:
                    reviewer = (review.get("user") or {}).get("login", "unknown")
                    submitted = review.get("submitted_at", "")
                    state = review.get("state", "")

                    if reviewer not in reviewer_stats:
                        reviewer_stats[reviewer] = {
                            "reviews_given": 0,
                            "approvals": 0,
                            "changes_requested": 0,
                            "comments": 0,
                            "turnaround_hours": [],
                        }

                    stats = reviewer_stats[reviewer]
                    stats["reviews_given"] += 1
                    if state == "APPROVED":
                        stats["approvals"] += 1
                    elif state == "CHANGES_REQUESTED":
                        stats["changes_requested"] += 1
                    elif state == "COMMENTED":
                        stats["comments"] += 1

                    if submitted:
                        reviewed_at = datetime.fromisoformat(submitted)
                        turnaround = (reviewed_at - pr_created).total_seconds() / 3600
                        if turnaround > 0:
                            stats["turnaround_hours"].append(turnaround)
    except httpx.HTTPStatusError as e:
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    # Compute averages
    reviewers = []
    for name, stats in reviewer_stats.items():
        entry: dict[str, Any] = {
            "reviewer": name,
            "reviews_given": stats["reviews_given"],
            "approvals": stats["approvals"],
            "changes_requested": stats["changes_requested"],
            "comments": stats["comments"],
        }
        if stats["turnaround_hours"]:
            hours = stats["turnaround_hours"]
            entry["avg_turnaround_hours"] = round(sum(hours) / len(hours), 1)
        reviewers.append(entry)

    reviewers.sort(key=lambda r: r["reviews_given"], reverse=True)

    return {
        "repo": repo,
        "days": days,
        "prs_analyzed": len(recent_prs),
        "reviewers": reviewers,
    }
