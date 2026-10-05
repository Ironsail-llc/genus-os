"""GitHub REST API tool handlers: dev-team metrics, pull-request diffs, review posting."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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
    """The GitHub credential, vault first.

    Through the accessor rather than the process environment, because the
    environment is a snapshot of a root-owned file taken at boot: an expired
    token in it used to shadow the replacement the assistant had just written
    into the vault, and no amount of rotating fixed that without a restart.
    """
    from robothor.secrets import get_secret

    return get_secret("GITHUB_TOKEN") or ""


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


_BOT_LOGIN_BASENAMES = frozenset(
    {
        "dependabot",
        "renovate",
        "github-actions",
        "release-please",
        "pre-commit-ci",
        "codecov",
        "codecov-commenter",
        "semantic-release",
    }
)


def _is_bot_login(login: str) -> bool:
    """Detect bot GitHub logins.

    Matches the conventional `<name>[bot]` suffix or a small allowlist of
    known bot base names (some bots author PRs from a user account without
    the [bot] suffix).
    """
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    return login.lower() in _BOT_LOGIN_BASENAMES


def _to_github_search_ts(iso: str) -> str:
    """Normalize an RFC3339/ISO timestamp to GitHub Search's `Z`-suffixed form.

    GitHub Search accepts `YYYY-MM-DDTHH:MM:SSZ`; the `+00:00` offset form
    is silently *ignored* by the qualifier parser, so we must rewrite.
    """
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _search_merged_pr_numbers(
    client: httpx.AsyncClient,
    repo: str,
    since_iso: str,
    until_iso: str,
    headers: dict[str, str],
    max_pages: int = 10,
) -> list[int]:
    """Return PR numbers merged in [since, until) via the GitHub Search API.

    GitHub Search does NOT combine `merged:>=A merged:<B` (only the last
    qualifier wins). Use the `merged:A..B` range form. The range is
    inclusive on both ends — subtract 1s from `until` so a PR merged
    exactly at the boundary doesn't double-count across week windows.
    """
    since = _to_github_search_ts(since_iso)
    until_dt = datetime.fromisoformat(until_iso) - timedelta(seconds=1)
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=UTC)
    until = until_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"repo:{repo} is:pr is:merged merged:{since}..{until}"
    numbers: list[int] = []
    next_url: str | None = f"{_GITHUB_API}/search/issues"
    params: dict[str, Any] | None = {"q": q, "per_page": 100}
    for _ in range(max_pages):
        if not next_url:
            break
        resp = await client.get(next_url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            num = item.get("number")
            if num is not None:
                numbers.append(num)
        link = resp.headers.get("Link", "")
        next_url = None
        params = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break
    return numbers


@_handler("github_pr_stats")
async def _github_pr_stats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Aggregated PR metrics for a repo over a date range.

    Uses the GitHub Search API to enumerate PRs merged in [since, until),
    then fans out per-PR detail fetches for cycle time / size / merged_by.
    Server-side date filtering means high-churn repos no longer undercount.
    """
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    repo = args.get("repo", "")
    if not repo:
        return {"error": "repo is required (format: owner/repo)"}

    since_str = args.get("since")
    if not since_str:
        return {"error": "since is required (RFC3339 timestamp)"}
    until_str = args.get("until") or datetime.now(UTC).isoformat()
    period = {"since": since_str, "until": until_str}

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            headers = _headers(token)
            numbers = await _search_merged_pr_numbers(client, repo, since_str, until_str, headers)
            if not numbers:
                return {
                    "repo": repo,
                    "period": period,
                    "merged_count": 0,
                    "bot_merged_count": 0,
                    "bot_authors": {},
                    "avg_cycle_time_hours": 0,
                    "median_cycle_time_hours": 0,
                    "avg_size_lines": 0,
                    "authors": {},
                    "merged_by": {},
                    "message": "No merged PRs in this period",
                }
            sem = asyncio.Semaphore(8)

            async def _fetch_detail(num: int) -> dict[str, Any] | None:
                async with sem:
                    try:
                        resp = await client.get(
                            f"{_GITHUB_API}/repos/{repo}/pulls/{num}", headers=headers
                        )
                        resp.raise_for_status()
                        data: dict[str, Any] = resp.json()
                        return data
                    except Exception:
                        return None

            details = await asyncio.gather(*[_fetch_detail(n) for n in numbers])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Repository {repo} not found"}
        return {"error": f"GitHub API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    merged_prs = [d for d in details if d and d.get("merged_at")]

    human_prs: list[dict[str, Any]] = []
    bot_authors: dict[str, int] = {}
    for pr in merged_prs:
        author = (pr.get("user") or {}).get("login", "unknown")
        if _is_bot_login(author):
            bot_authors[author] = bot_authors.get(author, 0) + 1
        else:
            human_prs.append(pr)

    cycle_times_hours: list[float] = []
    sizes: list[int] = []
    for pr in human_prs:
        if pr.get("draft"):
            continue
        try:
            created = datetime.fromisoformat(pr["created_at"])
            merged = datetime.fromisoformat(pr["merged_at"])
        except (KeyError, TypeError, ValueError):
            continue
        cycle_times_hours.append((merged - created).total_seconds() / 3600)
        sizes.append((pr.get("additions") or 0) + (pr.get("deletions") or 0))

    author_counts: dict[str, int] = {}
    for pr in human_prs:
        author = (pr.get("user") or {}).get("login", "unknown")
        author_counts[author] = author_counts.get(author, 0) + 1

    merged_by_counts: dict[str, int] = {}
    for pr in human_prs:
        merger = (pr.get("merged_by") or {}).get("login", "unknown")
        merged_by_counts[merger] = merged_by_counts.get(merger, 0) + 1

    return {
        "repo": repo,
        "period": period,
        "merged_count": len(human_prs),
        "bot_merged_count": sum(bot_authors.values()),
        "bot_authors": bot_authors,
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


# ── Pull-request diffs and review posting ──────────────────────────────
#
# Read tools (diff, files, compare) are read-only and offered by default. The
# three write tools (create_review, reply_review_comment, resolve_threads) are
# in OPT_IN_TOOLS: an agent is offered them only when its manifest names them
# in `tools_allowed`, and each refuses a benchmark run. The posting rules —
# anchor partition, severity split, body, own-PR fallback — are pure and live
# in robothor.pr_review.posting.

#: A diff larger than this is cut, with ``truncated: true`` saying so. Enough
#: for a large pull request; past it the agent should read per-file patches.
_DIFF_MAX_CHARS = 150_000
#: Shared patch budget for github_pr_files / github_compare output.
_PATCH_BUDGET_CHARS = 150_000

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERDICTS = frozenset({"APPROVE", "COMMENT", "REQUEST_CHANGES"})
_OWN_PR_ERROR_RE = re.compile(
    r"(can ?not|cannot) (approve|request changes on) your own pull request", re.IGNORECASE
)
_REVIEW_FOOTER = "<sub>Automated review</sub>"
#: Per-comment cap for inline review comments (the review body has its own).
_INLINE_COMMENT_MAX_CHARS = 8_000
#: A 422 is only an anchor problem (worth folding comments into the body) when
#: GitHub's message talks about where the comment goes.
_ANCHOR_422_RE = re.compile(r"line|path|diff|pull_request_review_thread|position", re.IGNORECASE)


def _client(timeout: float = 20.0) -> httpx.AsyncClient:
    """The HTTP client every review tool uses (tests swap in a fake transport)."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


def _valid_repo(repo: str) -> bool:
    return bool(_REPO_RE.match(repo)) and ".." not in repo


def _pr_args(args: dict[str, Any]) -> tuple[str, int] | dict[str, Any]:
    """``(repo, number)`` or an error dict. ``pr_number`` is accepted as an alias."""
    repo = str(args.get("repo") or "")
    number: Any = args.get("number", args.get("pr_number"))
    if not repo or number in (None, ""):
        return {"error": "repo and number are required"}
    if not _valid_repo(repo):
        return {"error": f"repo must be in owner/repo format, got {repo!r}"}
    try:
        num = int(number)
    except (TypeError, ValueError):
        return {"error": f"number must be an integer, got {number!r}"}
    if num <= 0:
        return {"error": f"number must be positive, got {num}"}
    return repo, num


def _error_detail(resp: httpx.Response) -> str:
    """GitHub's own words for a failure: message plus any ``errors`` entries."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    if not isinstance(data, dict):
        return str(data)[:500]
    parts = [str(data.get("message") or "")]
    parts.extend(
        err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err)
        for err in data.get("errors") or []
    )
    return "; ".join(p for p in parts if p)[:1000]


def _http_error(e: httpx.HTTPStatusError, what: str) -> dict[str, Any]:
    status = e.response.status_code
    if status == 404:
        return {"error": f"{what} not found"}
    if status == 401:
        return {"error": "GitHub authentication failed — check token"}
    return {"error": f"GitHub API error {status}: {_error_detail(e.response)}"}


def _slim_file(f: dict[str, Any], budget: list[int]) -> dict[str, Any]:
    """One changed file; its patch is dropped once the shared budget is spent."""
    patch = f.get("patch")
    out: dict[str, Any] = {
        "filename": f.get("filename", ""),
        "status": f.get("status", ""),
        "additions": f.get("additions", 0),
        "deletions": f.get("deletions", 0),
        "patch": None,
    }
    if f.get("previous_filename"):
        out["previous_filename"] = f["previous_filename"]
    if patch:
        if len(patch) <= budget[0]:
            out["patch"] = patch
            budget[0] -= len(patch)
        else:
            out["patch_omitted"] = True
    return out


async def _pr_patches(
    client: httpx.AsyncClient, repo: str, number: int, headers: dict[str, str]
) -> list[dict[str, Any]]:
    """Every changed file of a pull request (GitHub caps the list at 3,000)."""
    return await _paginate(
        client,
        f"{_GITHUB_API}/repos/{repo}/pulls/{number}/files",
        headers,
        {"per_page": 100},
        max_pages=30,
    )


@_handler("github_pr_diff")
async def _github_pr_diff(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The pull request's unified diff, capped at _DIFF_MAX_CHARS."""
    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    headers = {**_headers(token), "Accept": "application/vnd.github.diff"}
    try:
        async with _client(30.0) as client:
            resp = await client.get(f"{_GITHUB_API}/repos/{repo}/pulls/{number}", headers=headers)
            if resp.status_code == 406:
                return {
                    "error": "GitHub will not render this diff (too large); "
                    "use github_pr_files for per-file patches"
                }
            resp.raise_for_status()
            diff = resp.text
    except httpx.HTTPStatusError as e:
        return _http_error(e, f"PR #{number} in {repo}")
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    return {
        "repo": repo,
        "number": number,
        "diff": diff[:_DIFF_MAX_CHARS],
        "truncated": len(diff) > _DIFF_MAX_CHARS,
        "total_chars": len(diff),
    }


@_handler("github_pr_files")
async def _github_pr_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Changed files with status, line counts and per-file patch."""
    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    try:
        async with _client() as client:
            files = await _pr_patches(client, repo, number, _headers(token))
    except httpx.HTTPStatusError as e:
        return _http_error(e, f"PR #{number} in {repo}")
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    budget = [_PATCH_BUDGET_CHARS]
    slim = [_slim_file(f, budget) for f in files]
    return {
        "repo": repo,
        "number": number,
        "files": slim,
        "count": len(slim),
        "patches_omitted": sum(1 for f in slim if f.get("patch_omitted")),
    }


@_handler("github_compare")
async def _github_compare(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Compare two commits; diverged/behind/missing mean a full review is required."""
    from robothor.pr_review.posting import FULL_REVIEW_COMPARE_STATUSES

    repo = str(args.get("repo") or "")
    base = str(args.get("base") or "")
    head = str(args.get("head") or "")
    if not repo or not base or not head:
        return {"error": "repo, base and head are required"}
    if not _valid_repo(repo):
        return {"error": f"repo must be in owner/repo format, got {repo!r}"}
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    url = f"{_GITHUB_API}/repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
    try:
        async with _client(30.0) as client:
            resp = await client.get(url, headers=_headers(token))
            if resp.status_code == 404:
                # Before reading 404 as "the base was force-pushed away", confirm
                # the repo and head are reachable: a bad token or repo name 404s too.
                headers = _headers(token)
                repo_resp = await client.get(f"{_GITHUB_API}/repos/{repo}", headers=headers)
                if repo_resp.status_code != 200:
                    return {
                        "error": f"repository {repo} is not reachable "
                        f"(GitHub {repo_resp.status_code}) — check the repo name and token access"
                    }
                head_resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/commits/{quote(head, safe='')}", headers=headers
                )
                if head_resp.status_code != 200:
                    return {
                        "error": f"head commit {head} not found in {repo} "
                        f"(GitHub {head_resp.status_code})"
                    }
                return {
                    "repo": repo,
                    "base": base,
                    "head": head,
                    "status": "missing",
                    "full_review_required": True,
                    "commits": [],
                    "files": [],
                }
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return _http_error(e, f"comparison {base}...{head} in {repo}")
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    status = str(data.get("status") or "")
    raw_files = data.get("files") or []
    budget = [_PATCH_BUDGET_CHARS]
    commits = [
        {
            "sha": c.get("sha", ""),
            "message": str((c.get("commit") or {}).get("message") or "").split("\n", 1)[0],
            "author": ((c.get("commit") or {}).get("author") or {}).get("name", ""),
        }
        for c in data.get("commits") or []
    ]
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "status": status,
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "total_commits": data.get("total_commits", len(commits)),
        "full_review_required": status in FULL_REVIEW_COMPARE_STATUSES,
        # GitHub's compare lists at most 300 files and a page of commits.
        "files_truncated": len(raw_files) >= 300,
        "commits_truncated": len(commits) < int(data.get("total_commits") or len(commits)),
        "commits": commits,
        "files": [_slim_file(f, budget) for f in raw_files],
    }


def _validate_issues(raw: Any) -> list[dict[str, Any]] | str:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return "issues must be a list"
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return f"issues[{i}] must be an object"
        if not item.get("title") and not item.get("body"):
            return f"issues[{i}] needs a title or body"
        out.append(item)
    return out


async def _viewer_login(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    """The token's own login, or "" when the token cannot say (app tokens 403 here)."""
    try:
        resp = await client.get(f"{_GITHUB_API}/user", headers=headers)
        if resp.status_code != 200:
            return ""
        return str(resp.json().get("login") or "")
    except Exception:  # noqa: BLE001 - an unknown viewer defers to the 422 fallback
        return ""


async def _find_own_review(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    pr_url: str,
    viewer: str,
    commit_id: str,
) -> dict[str, Any] | None:
    """An automated review by this token at ``commit_id``, if one already exists.

    Matches on commit, the footer marker and (when the token can say who it is)
    the author. A failed lookup is "none found": posting is the safe default.
    """
    try:
        reviews = await _paginate(
            client, f"{pr_url}/reviews", headers, {"per_page": 100}, max_pages=10
        )
    except Exception:  # noqa: BLE001
        return None
    for r in reviews:
        if r.get("commit_id") != commit_id or _REVIEW_FOOTER not in str(r.get("body") or ""):
            continue
        login = str((r.get("user") or {}).get("login") or "")
        if viewer and login.lower() != viewer.lower():
            continue
        return r
    return None


async def _review_comments(
    client: httpx.AsyncClient, headers: dict[str, str], pr_url: str, review_id: Any
) -> list[dict[str, Any]]:
    """The inline comments a review posted, as ``{id, path, line}`` in posting order."""
    if review_id is None:
        return []
    try:
        cs = await _paginate(
            client,
            f"{pr_url}/reviews/{review_id}/comments",
            headers,
            {"per_page": 100},
            max_pages=10,
        )
    except Exception:  # noqa: BLE001 - the review is posted; ids are a convenience
        return []
    return [
        {"id": c["id"], "path": c.get("path", ""), "line": c.get("line")} for c in cs if "id" in c
    ]


async def _already_posted(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    pr_url: str,
    repo: str,
    number: int,
    verdict: str,
    commit_id: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    rid = review.get("id")
    comments = await _review_comments(client, headers, pr_url, rid)
    return {
        "repo": repo,
        "number": number,
        "review_id": rid,
        "url": review.get("html_url")
        or f"https://github.com/{repo}/pull/{number}#pullrequestreview-{rid}",
        "verdict": verdict,
        "already_posted": True,
        "comment_ids": [c["id"] for c in comments],
        "comments": comments,
        "commit_id": commit_id,
    }


@_handler("github_create_review")
async def _github_create_review(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Post a review: blocking findings inline where the diff allows, the rest in the body."""
    from robothor.pr_review.policy import guard_posted_verdict
    from robothor.pr_review.posting import (
        compose_body,
        format_issue_comment,
        own_pr_body,
        partition_comments,
        split_by_severity,
    )

    if ctx.is_benchmark:
        return {"error": "github_create_review is refused on a benchmark run"}
    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    verdict = str(args.get("verdict") or "").upper()
    if verdict not in _VERDICTS:
        return {"error": f"verdict must be one of {sorted(_VERDICTS)}, got {verdict!r}"}
    issues = _validate_issues(args.get("issues"))
    if isinstance(issues, str):
        return {"error": issues}
    # Every caller crosses this: an APPROVE beside a blocker/major finding is
    # never posted, whoever asked for it (robothor.pr_review.policy).
    verdict, verdict_overridden = guard_posted_verdict(verdict, issues)
    prior = args.get("prior_issues") or []
    if not isinstance(prior, list) or not all(isinstance(p, dict) for p in prior):
        return {"error": "prior_issues must be a list of objects"}
    summary = str(args.get("summary") or "")
    commit_id = str(args.get("commit_id") or "")
    if not commit_id:
        return {"error": "commit_id is required (the head SHA the review was written against)"}
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    headers = _headers(token)
    pr_url = f"{_GITHUB_API}/repos/{repo}/pulls/{number}"
    try:
        async with _client(30.0) as client:
            resp = await client.get(pr_url, headers=headers)
            resp.raise_for_status()
            pr = resp.json()
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            if head_sha and head_sha != commit_id:
                # Anchors are checked against the CURRENT diff; posting them on an
                # older commit would land them on the wrong lines or 422.
                return {
                    "error": "PR head moved since the review was written; re-review the new head",
                    "head_sha": head_sha,
                    "commit_id": commit_id,
                }
            author = str((pr.get("user") or {}).get("login") or "")
            viewer = await _viewer_login(client, headers)
            files = await _pr_patches(client, repo, number, headers)
            patches = {str(f.get("filename")): f.get("patch") for f in files}

            existing = await _find_own_review(client, headers, pr_url, viewer, commit_id)
            if existing is not None:
                return await _already_posted(
                    client, headers, pr_url, repo, number, verdict, commit_id, existing
                )

            # Re-read the head: a push while the files were read leaves anchors stale.
            recheck = await client.get(pr_url, headers=headers)
            recheck.raise_for_status()
            now_sha = str((recheck.json().get("head") or {}).get("sha") or "")
            if now_sha and now_sha != commit_id:
                return {
                    "error": "PR head moved since the review was written; re-review the new head",
                    "head_sha": now_sha,
                    "commit_id": commit_id,
                }

            blocking, non_blocking = split_by_severity(issues)
            proposed = [{**i, "body": format_issue_comment(i), "_issue": i} for i in blocking]
            inline, unanchored = partition_comments(proposed, patches)
            for c in inline:
                if len(c["body"]) > _INLINE_COMMENT_MAX_CHARS:
                    c["body"] = c["body"][: _INLINE_COMMENT_MAX_CHARS - 1] + "…"
            out_of_diff = [c["_issue"] for c in unanchored]

            def _body(blocking_in_body: list[Any]) -> str:
                return compose_body(
                    summary, blocking_in_body, non_blocking, prior, footer=_REVIEW_FOOTER
                )

            body = _body(out_of_diff)
            event = verdict
            as_comment = False
            if verdict != "COMMENT" and author and viewer and author.lower() == viewer.lower():
                # GitHub refuses APPROVE / REQUEST_CHANGES on your own pull request.
                event, body, as_comment = "COMMENT", own_pr_body(verdict, body), True
            anchors_folded = False

            # At most three posts: as decided, the own-PR fallback, anchors folded.
            review: dict[str, Any] | None = None
            last_error = ""
            for _attempt in range(3):
                payload: dict[str, Any] = {
                    "commit_id": commit_id,
                    "event": event,
                    # COMMENT and REQUEST_CHANGES 422 on an empty body.
                    "body": body or ("" if event == "APPROVE" else "No findings."),
                    "comments": inline,
                }
                try:
                    resp = await client.post(f"{pr_url}/reviews", headers=headers, json=payload)
                except httpx.TransportError as exc:
                    # Timeout or reset: the review may have been created anyway.
                    landed = await _find_own_review(client, headers, pr_url, viewer, commit_id)
                    if landed is not None:
                        return await _already_posted(
                            client, headers, pr_url, repo, number, verdict, commit_id, landed
                        )
                    return {"error": f"GitHub request failed: {exc!r}"}
                if resp.status_code < 400:
                    review = resp.json()
                    break
                if resp.status_code >= 500:
                    landed = await _find_own_review(client, headers, pr_url, viewer, commit_id)
                    if landed is not None:
                        return await _already_posted(
                            client, headers, pr_url, repo, number, verdict, commit_id, landed
                        )
                if resp.status_code != 422:
                    resp.raise_for_status()
                last_error = _error_detail(resp)
                if event != "COMMENT" and _OWN_PR_ERROR_RE.search(last_error):
                    event, body, as_comment = "COMMENT", own_pr_body(verdict, body), True
                    continue
                if inline and not anchors_folded and _ANCHOR_422_RE.search(last_error):
                    # One invalid anchor sinks the whole review: fold every finding
                    # into the body and try exactly once more.
                    body = _body(list(blocking))
                    if as_comment:
                        body = own_pr_body(verdict, body)
                    inline, anchors_folded = [], True
                    continue
                break
            if review is None:
                return {"error": f"GitHub API error 422: {last_error}"}

            review_id = review.get("id")
            comment_ids: list[int] = []
            posted_comments: list[dict[str, Any]] = []
            if inline and review_id is not None:
                posted_comments = await _review_comments(client, headers, pr_url, review_id)
                comment_ids = [c["id"] for c in posted_comments]
    except httpx.HTTPStatusError as e:
        return _http_error(e, f"PR #{number} in {repo}")
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    return {
        "repo": repo,
        "number": number,
        "review_id": review_id,
        "url": review.get("html_url")
        or f"https://github.com/{repo}/pull/{number}#pullrequestreview-{review_id}",
        "verdict": verdict,
        "verdict_overridden": verdict_overridden,
        "event": event,
        "posted_as_comment": as_comment,
        "inline_count": len(inline),
        "body_findings": len(blocking) - len(inline) + len(non_blocking),
        "anchors_folded": anchors_folded,
        "already_posted": False,
        "comment_ids": comment_ids,
        "comments": posted_comments,
        "commit_id": commit_id,
    }


@_handler("github_reply_review_comment")
async def _github_reply_review_comment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Reply inside an existing review-comment thread."""
    if ctx.is_benchmark:
        return {"error": "github_reply_review_comment is refused on a benchmark run"}
    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    body = str(args.get("body") or "")
    if not body.strip():
        return {"error": "body is required"}
    try:
        comment_id = int(args.get("comment_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {"error": "comment_id must be an integer"}
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    try:
        async with _client() as client:
            resp = await client.post(
                f"{_GITHUB_API}/repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
                headers=_headers(token),
                json={"body": body},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return _http_error(e, f"review comment {comment_id} on PR #{number} in {repo}")
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    return {
        "repo": repo,
        "number": number,
        "comment_id": data.get("id"),
        "in_reply_to_id": data.get("in_reply_to_id", comment_id),
        "url": data.get("html_url", ""),
    }


_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes { databaseId author { login } pullRequestReview { databaseId } }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($id: ID!) {
  resolveReviewThread(input: { threadId: $id }) { thread { id isResolved } }
}
"""


class _GraphQLError(Exception):
    """GitHub answered 200 with an ``errors`` array."""


async def _graphql(
    client: httpx.AsyncClient, headers: dict[str, str], query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    resp = await client.post(
        f"{_GITHUB_API}/graphql", headers=headers, json={"query": query, "variables": variables}
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) for e in data["errors"])
        raise _GraphQLError(f"GitHub GraphQL error: {msgs}")
    result: dict[str, Any] = data.get("data") or {}
    return result


@_handler("github_resolve_threads")
async def _github_resolve_threads(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Resolve the review threads that the given reviews opened — and no others.

    A thread belongs to the review its FIRST comment was posted in. Matching on
    review id rather than on author login is the point: a person writing by
    hand from the same account opens threads under a different review, and
    those are theirs to resolve.
    """
    if ctx.is_benchmark:
        return {"error": "github_resolve_threads is refused on a benchmark run"}
    parsed = _pr_args(args)
    if isinstance(parsed, dict):
        return parsed
    repo, number = parsed
    raw_ids = args.get("review_ids") or []
    if not isinstance(raw_ids, list):
        return {"error": "review_ids must be a list of integer review ids"}
    try:
        review_ids = {int(r) for r in raw_ids}
    except (TypeError, ValueError):
        return {"error": "review_ids must be a list of integer review ids"}
    if not review_ids:
        return {"error": "review_ids is required — only threads from these reviews are resolved"}
    token = _get_token()
    if not token:
        return {"error": "GITHUB_TOKEN not configured"}

    owner, name = repo.split("/", 1)
    headers = _headers(token)
    to_resolve: list[str] = []
    already = 0
    try:
        async with _client(30.0) as client:
            after: str | None = None
            for _ in range(20):  # 2,000 threads is far past any real pull request
                data = await _graphql(
                    client,
                    headers,
                    _THREADS_QUERY,
                    {"owner": owner, "repo": name, "number": number, "after": after},
                )
                threads = ((data.get("repository") or {}).get("pullRequest") or {}).get(
                    "reviewThreads"
                )
                if not threads:
                    return {"error": f"PR #{number} in {repo} not found"}
                for t in threads.get("nodes") or []:
                    first = ((t.get("comments") or {}).get("nodes") or [None])[0] or {}
                    review = first.get("pullRequestReview") or {}
                    if review.get("databaseId") not in review_ids:
                        continue
                    if t.get("isResolved"):
                        already += 1
                    else:
                        to_resolve.append(str(t["id"]))
                page = threads.get("pageInfo") or {}
                after = page.get("endCursor") if page.get("hasNextPage") else None
                if not after:
                    break
            for thread_id in to_resolve:
                await _graphql(client, headers, _RESOLVE_MUTATION, {"id": thread_id})
    except httpx.HTTPStatusError as e:
        return _http_error(e, f"PR #{number} in {repo}")
    except _GraphQLError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"GitHub request failed: {e}"}

    return {
        "repo": repo,
        "number": number,
        "resolved": len(to_resolve),
        "thread_ids": to_resolve,
        "already_resolved": already,
        "review_ids": sorted(review_ids),
    }
