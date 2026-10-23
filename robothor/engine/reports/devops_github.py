"""Deterministic weekly GitHub facts for the devops report.

One window — the report week ``[start, end)`` — for every metric, collected
from the core REST API (no Search API: its secondary rate limit is what made
whole repos vanish from earlier reports). Per repo it records raw facts by
GitHub login; the aggregate step maps logins to roster members.

* ``merged_prs`` — every PR merged in the window into ANY base branch, with
  gross and filtered (lockfiles/generated/vendored excluded) line counts and
  the logins that reviewed it.
* ``reviews`` — every review SUBMITTED in the window, self-reviews and
  pending reviews excluded, with turnaround hours when it is the reviewer's
  first review on that PR.
* ``commits`` — non-merge commits on the default branch, by login, or by git
  author email under ``unlinked`` when GitHub could not link a login.
* ``stale_prs`` — open, non-draft PRs older than ``STALE_DAYS``.

Failure is loud: an endpoint that still fails after retries adds an entry to
``errors`` and names the affected metric in the repo's ``unavailable`` map,
so the report shows "unavailable" rather than a confident zero. Nothing is
dropped silently.
"""

from __future__ import annotations

import asyncio
import fnmatch
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

GITHUB_API = "https://api.github.com"

#: Paths whose line changes are not engineering output: lockfiles, generated,
#: vendored and minified files. Basename globs match the file name; globs with
#: a ``/`` match a directory anywhere in the path.
EXCLUDED_PATH_GLOBS: tuple[str, ...] = (
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "composer.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "*.pb.go",
    "dist/**",
    "build/**",
    "vendor/**",
    "node_modules/**",
    "**/__generated__/**",
)

METRICS = ("prs", "lines", "commits", "reviews", "stale")
STALE_DAYS = 14
_RETRYABLE = {403, 429, 500, 502, 503, 504}
_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}


def is_excluded_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    dirs = parts[:-1]
    for glob in EXCLUDED_PATH_GLOBS:
        if "/" not in glob:
            if fnmatch.fnmatch(name, glob):
                return True
            continue
        segment = glob.replace("**/", "").replace("/**", "")
        if segment in dirs:
            return True
    return False


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class FetchError(Exception):
    def __init__(self, endpoint: str, status: int | None, message: str) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.message = message


class _Fetcher:
    """GETs with bounded concurrency, retry on transient errors, a retry budget."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        *,
        concurrency: int,
        retry_delays: Sequence[float],
        retry_budget: float,
    ) -> None:
        self.client = client
        self.headers = headers
        self.sem = asyncio.Semaphore(concurrency)
        self.delays = tuple(retry_delays)
        self.deadline = time.monotonic() + retry_budget

    async def _get(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        endpoint = url.replace(GITHUB_API, "")
        attempt = 0
        while True:
            status: int | None = None
            message = ""
            try:
                async with self.sem:
                    resp = await self.client.get(url, headers=self.headers, params=params)
                if resp.status_code < 400:
                    return resp
                status = resp.status_code
                try:
                    message = str(resp.json().get("message", ""))[:200]
                except Exception:  # noqa: BLE001 - non-JSON error body
                    message = resp.text[:200]
                retryable = status in _RETRYABLE
                retry_after = resp.headers.get("Retry-After")
            except httpx.HTTPError as e:
                message = f"{type(e).__name__}: {e}"
                retryable = True
                retry_after = None
            if not retryable or attempt >= len(self.delays) or time.monotonic() >= self.deadline:
                raise FetchError(endpoint, status, message or f"HTTP {status}")
            delay = self.delays[attempt]
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(float(retry_after), 30.0))
            attempt += 1
            await asyncio.sleep(delay)

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._get(url, params)).json()

    async def get_all(
        self,
        url: str,
        params: dict[str, Any],
        stop: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Every page (Link: rel=next). ``stop(item)`` true on a page's last item ends it."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params: dict[str, Any] | None = params
        while next_url:
            resp = await self._get(next_url, next_params)
            page = resp.json()
            if not isinstance(page, list):
                raise FetchError(url.replace(GITHUB_API, ""), resp.status_code, "expected a list")
            items.extend(page)
            if stop and page and stop(page[-1]):
                break
            next_url = None
            next_params = None
            for part in resp.headers.get("Link", "").split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
        return items


def _line_counts(files: list[dict[str, Any]]) -> dict[str, int]:
    """Gross and filtered (generated/lockfiles excluded) line changes of a PR's files."""
    add = dele = add_all = del_all = 0
    for fl in files:
        a, d = int(fl.get("additions") or 0), int(fl.get("deletions") or 0)
        add_all += a
        del_all += d
        if not is_excluded_path(fl.get("filename") or ""):
            add += a
            dele += d
    return {
        "additions": add_all,
        "deletions": del_all,
        "lines_added": add,
        "lines_deleted": dele,
        "excluded_lines": (add_all - add) + (del_all - dele),
    }


def _reviews_in_window(
    pr: dict[str, Any], raw: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Reviews on ``pr`` submitted in [start, end); self-reviews and pending excluded.

    ``turnaround_h`` is set only on the reviewer's first review of the PR.
    """
    author = (pr.get("user") or {}).get("login", "")
    created = _ts(pr.get("created_at"))
    valid = []
    for r in raw:
        login = (r.get("user") or {}).get("login", "")
        when = _ts(r.get("submitted_at"))
        if r.get("state") in _REVIEW_STATES and login and when is not None:
            valid.append((login, when, r))
    first: dict[str, datetime] = {}
    for login, when, _ in valid:
        if login not in first or when < first[login]:
            first[login] = when
    out = []
    for login, when, r in valid:
        if login == author or not (start <= when < end):
            continue
        turnaround = None
        if first[login] == when and created is not None:
            turnaround = round((when - created).total_seconds() / 3600, 1)
        out.append(
            {
                "pr": pr["number"],
                "pr_author": author,
                "reviewer": login,
                "state": r.get("state"),
                "submitted_at": r.get("submitted_at"),
                "turnaround_h": turnaround,
            }
        )
    return out


def _reviewer_logins(raw: list[dict[str, Any]], author: str) -> list[str]:
    """Everyone but the author who reviewed the PR, at any time."""
    return sorted(
        {
            (r.get("user") or {}).get("login", "")
            for r in raw
            if r.get("state") in _REVIEW_STATES
            and (r.get("user") or {}).get("login") not in (None, "", author)
        }
    )


def _commit_counts(items: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Non-merge commits by login; unlinked ones by author email."""
    by_login: dict[str, int] = {}
    unlinked: dict[str, int] = {}
    for c in items:
        if len(c.get("parents") or []) > 1:
            continue
        login = (c.get("author") or {}).get("login")
        if login:
            by_login[login] = by_login.get(login, 0) + 1
        else:
            email = ((c.get("commit") or {}).get("author") or {}).get("email") or "unknown"
            unlinked[email] = unlinked.get(email, 0) + 1
    return {"by_login": by_login, "unlinked": unlinked}


async def _collect_repo(
    f: _Fetcher, repo: str, start: datetime, end: datetime, now: datetime
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out: dict[str, Any] = {
        "default_branch": None,
        "unavailable": {},
        "merged_prs": [],
        "reviews": [],
        "commits": {"by_login": {}, "unlinked": {}},
        "stale_prs": [],
    }
    errors: list[dict[str, Any]] = []
    base = f"{GITHUB_API}/repos/{repo}"

    def fail(metrics: Sequence[str], e: FetchError) -> None:
        errors.append(
            {"repo": repo, "endpoint": e.endpoint, "status": e.status, "message": e.message}
        )
        for m in metrics:
            out["unavailable"].setdefault(m, f"{e.endpoint}: {e.status or ''} {e.message}".strip())

    try:
        meta = await f.get_json(base)
        out["default_branch"] = meta.get("default_branch") or "main"
    except FetchError as e:
        fail(METRICS, e)
        return out, errors

    async def prs_and_reviews() -> None:
        try:
            candidates = await f.get_all(
                f"{base}/pulls",
                {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100},
                stop=lambda pr: (_ts(pr.get("updated_at")) or start) < start,
            )
        except FetchError as e:
            fail(("prs", "lines", "reviews"), e)
            return
        touched = [
            pr
            for pr in candidates
            if (_ts(pr.get("updated_at")) or start) >= start
            and (_ts(pr.get("created_at")) or start) < end
        ]
        merged = [
            pr
            for pr in touched
            if pr.get("merged_at") and start <= (_ts(pr["merged_at"]) or start) < end
        ]
        reviews_by_pr: dict[int, list[dict[str, Any]] | None] = {}

        async def fetch_reviews(pr: dict[str, Any]) -> None:
            n = pr["number"]
            try:
                reviews_by_pr[n] = await f.get_all(f"{base}/pulls/{n}/reviews", {"per_page": 100})
            except FetchError as e:
                fail(("reviews",), e)
                reviews_by_pr[n] = None

        async def fetch_merged(pr: dict[str, Any]) -> dict[str, Any]:
            n = pr["number"]
            rec: dict[str, Any] = {
                "number": n,
                "title": (pr.get("title") or "")[:120],
                "author": (pr.get("user") or {}).get("login", ""),
                "base": (pr.get("base") or {}).get("ref", ""),
                "created_at": pr.get("created_at"),
                "merged_at": pr.get("merged_at"),
                "merged_by": None,
                "additions": None,
                "deletions": None,
                "changed_files": None,
                "lines_added": None,
                "lines_deleted": None,
                "excluded_lines": None,
            }
            try:
                detail = await f.get_json(f"{base}/pulls/{n}")
                rec["merged_by"] = (detail.get("merged_by") or {}).get("login")
                rec["changed_files"] = detail.get("changed_files")
            except FetchError as e:
                fail(("lines",), e)
            try:
                files = await f.get_all(f"{base}/pulls/{n}/files", {"per_page": 100})
            except FetchError as e:
                fail(("lines",), e)
                return rec
            rec.update(_line_counts(files))
            if rec["changed_files"] is None:
                rec["changed_files"] = len(files)
            return rec

        results = await asyncio.gather(
            *[fetch_merged(pr) for pr in merged], *[fetch_reviews(pr) for pr in touched]
        )
        merged_recs = results[: len(merged)]

        for pr in touched:
            raw = reviews_by_pr.get(pr["number"])
            if raw is not None:
                out["reviews"].extend(_reviews_in_window(pr, raw, start, end))
        for rec in merged_recs:
            raw = reviews_by_pr.get(rec["number"])
            rec["reviewers"] = None if raw is None else _reviewer_logins(raw, rec["author"])
        out["merged_prs"] = sorted(merged_recs, key=lambda p: p["number"])

    async def commits() -> None:
        try:
            items = await f.get_all(
                f"{base}/commits",
                {
                    "sha": out["default_branch"],
                    "since": start.astimezone(UTC).isoformat(),
                    "until": end.astimezone(UTC).isoformat(),
                    "per_page": 100,
                },
            )
        except FetchError as e:
            fail(("commits",), e)
            return
        out["commits"] = _commit_counts(items)

    async def stale() -> None:
        try:
            items = await f.get_all(f"{base}/pulls", {"state": "open", "per_page": 100})
        except FetchError as e:
            fail(("stale",), e)
            return
        cutoff = now - timedelta(days=STALE_DAYS)
        for pr in items:
            created = _ts(pr.get("created_at"))
            if pr.get("draft") or created is None or created >= cutoff:
                continue
            out["stale_prs"].append(
                {
                    "number": pr["number"],
                    "title": (pr.get("title") or "")[:80],
                    "author": (pr.get("user") or {}).get("login", ""),
                    "age_days": round((now - created).total_seconds() / 86400, 1),
                }
            )
        out["stale_prs"].sort(key=lambda p: -p["age_days"])

    await asyncio.gather(prs_and_reviews(), commits(), stale())
    return out, errors


async def collect_github(
    repos: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str] | None = None,
    now: datetime | None = None,
    concurrency: int = 6,
    retry_delays: Sequence[float] = (2.0, 5.0, 10.0),
    retry_budget: float = 120.0,
) -> dict[str, Any]:
    """Collect the week's facts for every repo. Never raises on a GitHub error."""
    f = _Fetcher(
        client,
        headers or {},
        concurrency=concurrency,
        retry_delays=retry_delays,
        retry_budget=retry_budget,
    )
    now = now or datetime.now(UTC)
    results = await asyncio.gather(*[_collect_repo(f, r, start, end, now) for r in repos])
    data: dict[str, Any] = {"repos": {}, "errors": []}
    for repo, (repo_out, errors) in zip(repos, results, strict=True):
        data["repos"][repo] = repo_out
        data["errors"].extend(errors)
    data["complete"] = not data["errors"]
    return data
