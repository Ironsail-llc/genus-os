"""Tests for the deterministic weekly GitHub collector (devops report)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from robothor.engine.reports.devops_github import (
    EXCLUDED_PATH_GLOBS,
    collect_github,
    is_excluded_path,
)

START = datetime(2026, 5, 11, 4, 0, tzinfo=UTC)
END = datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
NOW = datetime(2026, 5, 18, 13, 0, tzinfo=UTC)
IN = "2026-05-13T15:00:00Z"
BEFORE = "2026-05-08T15:00:00Z"
REPO = "acme/widgets"


def _pr(n, author, *, merged_at=IN, updated_at=IN, created_at=BEFORE, base="main", state="closed"):
    return {
        "number": n,
        "title": f"PR {n}",
        "user": {"login": author},
        "state": state,
        "draft": False,
        "created_at": created_at,
        "updated_at": updated_at,
        "merged_at": merged_at,
        "base": {"ref": base},
    }


class FakeGitHub:
    """A tiny GitHub REST double: paginates by `page`, emits Link headers."""

    def __init__(self):
        self.pulls_all: list[dict] = []
        self.pulls_open: list[dict] = []
        self.details: dict[int, dict] = {}
        self.files: dict[int, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.commits: list[dict] = []
        self.fail: dict[str, list[int]] = {}  # path -> statuses to return in order
        self.calls: list[str] = []

    def _page(self, request, items):
        q = parse_qs(urlparse(str(request.url)).query)
        per = int(q.get("per_page", ["30"])[0])
        page = int(q.get("page", ["1"])[0])
        chunk = items[(page - 1) * per : page * per]
        headers = {}
        if page * per < len(items):
            url = request.url.copy_merge_params({"page": str(page + 1)})
            headers["Link"] = f'<{url}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        statuses = self.fail.get(path)
        if statuses:
            return httpx.Response(statuses.pop(0), json={"message": "boom"})
        q = parse_qs(urlparse(str(request.url)).query)
        base = f"/repos/{REPO}"
        if path == base:
            return httpx.Response(200, json={"default_branch": "main"})
        if path == f"{base}/pulls":
            items = self.pulls_open if q.get("state") == ["open"] else self.pulls_all
            return self._page(request, items)
        if path == f"{base}/commits":
            return self._page(request, self.commits)
        parts = path[len(base) + 1 :].split("/")
        if parts[0] == "pulls":
            n = int(parts[1])
            if len(parts) == 2:
                return httpx.Response(200, json=self.details.get(n, {"number": n}))
            if parts[2] == "files":
                return self._page(request, self.files.get(n, []))
            if parts[2] == "reviews":
                return self._page(request, self.reviews.get(n, []))
        return httpx.Response(404, json={"message": "not found"})


async def _run(fake: FakeGitHub, **kw):
    transport = httpx.MockTransport(fake.handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.com") as c:
        return await collect_github(
            [REPO], START, END, client=c, now=NOW, retry_delays=(0, 0), **kw
        )


def _review(user, state, submitted):
    return {"user": {"login": user}, "state": state, "submitted_at": submitted}


class TestExcludedPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "pnpm-lock.yaml",
            "web/package-lock.json",
            "Cargo.lock",
            "go.sum",
            "static/app.min.js",
            "dist/bundle.js",
            "vendor/lib/x.php",
            "src/__generated__/types.ts",
            "api/foo.pb.go",
            "tests/__snapshots__/a.snap",
        ],
    )
    def test_generated_paths_excluded(self, path):
        assert is_excluded_path(path)

    @pytest.mark.parametrize("path", ["src/app.py", "lockscreen.py", "distance.go"])
    def test_source_kept(self, path):
        assert not is_excluded_path(path)

    def test_globs_are_a_module_constant(self):
        assert "*.lock" in EXCLUDED_PATH_GLOBS


class TestMergedPrs:
    @pytest.mark.asyncio
    async def test_pagination_beyond_fifty_and_any_base_branch(self):
        fake = FakeGitHub()
        fake.pulls_all = [
            _pr(i, "alice-gh", base="develop" if i % 2 else "main") for i in range(1, 131)
        ]
        fake.pulls_all.append(_pr(999, "alice-gh", merged_at=BEFORE, updated_at=BEFORE))
        out = await _run(fake)
        repo = out["repos"][REPO]
        assert len(repo["merged_prs"]) == 130
        assert {p["base"] for p in repo["merged_prs"]} == {"main", "develop"}
        assert out["errors"] == []

    @pytest.mark.asyncio
    async def test_lines_exclude_lockfiles(self):
        fake = FakeGitHub()
        fake.pulls_all = [_pr(1, "alice-gh")]
        fake.details[1] = {"number": 1, "merged_by": {"login": "bob-gh"}, "changed_files": 2}
        fake.files[1] = [
            {"filename": "src/a.py", "additions": 10, "deletions": 3},
            {"filename": "pnpm-lock.yaml", "additions": 5000, "deletions": 4000},
        ]
        out = await _run(fake)
        pr = out["repos"][REPO]["merged_prs"][0]
        assert pr["lines_added"] == 10
        assert pr["lines_deleted"] == 3
        assert pr["merged_by"] == "bob-gh"
        assert pr["excluded_lines"] == 9000

    @pytest.mark.asyncio
    async def test_detail_failure_is_an_error_not_a_dropped_pr(self):
        fake = FakeGitHub()
        fake.pulls_all = [_pr(1, "alice-gh"), _pr(2, "alice-gh")]
        fake.fail[f"/repos/{REPO}/pulls/2/files"] = [500, 500, 500]
        out = await _run(fake)
        repo = out["repos"][REPO]
        assert len(repo["merged_prs"]) == 2  # still counted
        assert "lines" in repo["unavailable"]
        assert any(e["endpoint"].endswith("/pulls/2/files") for e in out["errors"])
        assert out["complete"] is False

    @pytest.mark.asyncio
    async def test_transient_403_is_retried(self):
        fake = FakeGitHub()
        fake.pulls_all = [_pr(1, "alice-gh")]
        fake.fail[f"/repos/{REPO}/pulls/1"] = [403]
        out = await _run(fake)
        assert out["errors"] == []
        assert out["complete"] is True


class TestFailures:
    @pytest.mark.asyncio
    async def test_auth_failure_marks_repo_unavailable_not_zero(self):
        fake = FakeGitHub()
        fake.fail[f"/repos/{REPO}"] = [401]
        out = await _run(fake)
        repo = out["repos"][REPO]
        assert set(repo["unavailable"]) >= {"prs", "lines", "commits", "reviews", "stale"}
        assert out["errors"][0]["status"] == 401
        assert out["complete"] is False

    @pytest.mark.asyncio
    async def test_commit_listing_failure_marks_only_commits(self):
        fake = FakeGitHub()
        fake.fail[f"/repos/{REPO}/commits"] = [502, 502, 502]
        out = await _run(fake)
        repo = out["repos"][REPO]
        assert list(repo["unavailable"]) == ["commits"]


class TestReviews:
    @pytest.mark.asyncio
    async def test_window_and_self_review_filtering(self):
        fake = FakeGitHub()
        fake.pulls_all = [_pr(1, "alice-gh", created_at="2026-05-12T10:00:00Z")]
        fake.reviews[1] = [
            _review("bob-gh", "COMMENTED", BEFORE),  # before window: excluded
            _review("bob-gh", "APPROVED", "2026-05-12T14:00:00Z"),
            _review("alice-gh", "COMMENTED", IN),  # self-review: excluded
            _review("carol-gh", "CHANGES_REQUESTED", "2026-05-13T10:00:00Z"),
            _review("dave-gh", "PENDING", IN),
        ]
        out = await _run(fake)
        reviews = out["repos"][REPO]["reviews"]
        got = sorted((r["reviewer"], r["state"]) for r in reviews)
        assert got == [("bob-gh", "APPROVED"), ("carol-gh", "CHANGES_REQUESTED")]
        assert all(r["pr_author"] == "alice-gh" for r in reviews)
        carol = next(r for r in reviews if r["reviewer"] == "carol-gh")
        assert carol["turnaround_h"] == 24.0
        bob = next(r for r in reviews if r["reviewer"] == "bob-gh")
        # bob's FIRST review on the PR was before the window — no turnaround here
        assert bob["turnaround_h"] is None

    @pytest.mark.asyncio
    async def test_reviews_found_on_unmerged_prs_beyond_fifty(self):
        fake = FakeGitHub()
        fake.pulls_all = [
            _pr(i, "alice-gh", merged_at=None, state="open") for i in range(1, 76)
        ]
        for i in range(1, 76):
            fake.reviews[i] = [_review("bob-gh", "APPROVED", IN)]
        out = await _run(fake)
        assert len(out["repos"][REPO]["reviews"]) == 75

    @pytest.mark.asyncio
    async def test_merged_pr_reviewers_recorded(self):
        fake = FakeGitHub()
        fake.pulls_all = [_pr(1, "alice-gh")]
        fake.reviews[1] = [_review("alice-gh", "COMMENTED", IN)]
        out = await _run(fake)
        assert out["repos"][REPO]["merged_prs"][0]["reviewers"] == []


class TestCommits:
    @pytest.mark.asyncio
    async def test_merge_commits_excluded_and_unlinked_grouped_by_email(self):
        fake = FakeGitHub()
        fake.commits = [
            {"sha": "a", "author": {"login": "alice-gh"}, "parents": [{}],
             "commit": {"author": {"email": "a@example.com"}}},
            {"sha": "b", "author": {"login": "alice-gh"}, "parents": [{}, {}],
             "commit": {"author": {"email": "a@example.com"}}},
            {"sha": "c", "author": None, "parents": [{}],
             "commit": {"author": {"email": "ghost@example.com"}}},
        ]
        out = await _run(fake)
        commits = out["repos"][REPO]["commits"]
        assert commits["by_login"] == {"alice-gh": 1}
        assert commits["unlinked"] == {"ghost@example.com": 1}


class TestStale:
    @pytest.mark.asyncio
    async def test_open_prs_older_than_14_days(self):
        fake = FakeGitHub()
        fake.pulls_open = [
            _pr(1, "alice-gh", merged_at=None, state="open", created_at="2026-04-01T00:00:00Z"),
            _pr(2, "bob-gh", merged_at=None, state="open", created_at="2026-05-15T00:00:00Z"),
        ]
        out = await _run(fake)
        stale = out["repos"][REPO]["stale_prs"]
        assert [p["number"] for p in stale] == [1]
        assert stale[0]["author"] == "alice-gh"
