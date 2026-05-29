"""Tests for GitHub REST API tool handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")

# ─── Tool registration ──────────────────────────────────────────────


class TestGithubToolSchemas:
    """Verify GitHub API tools are registered in the tool registry."""

    def test_github_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for tool_name in [
                "github_list_prs",
                "github_get_pr",
                "github_pr_stats",
                "github_commit_activity",
                "github_review_stats",
            ]:
                assert tool_name in registry._schemas, f"{tool_name} not in registry"

    def test_github_tools_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        for tool_name in [
            "github_list_prs",
            "github_get_pr",
            "github_pr_stats",
            "github_commit_activity",
            "github_review_stats",
        ]:
            assert tool_name in READONLY_TOOLS, f"{tool_name} not in READONLY_TOOLS"

    def test_github_tools_in_set(self):
        from robothor.engine.tools import GITHUB_API_TOOLS

        assert len(GITHUB_API_TOOLS) == 5
        assert "github_list_prs" in GITHUB_API_TOOLS
        assert "github_review_stats" in GITHUB_API_TOOLS


# ─── Response slimming ──────────────────────────────────────────────


class TestSlimPr:
    def test_slim_pr_extracts_fields(self):
        from robothor.engine.tools.handlers.github_api import _slim_pr

        raw = {
            "number": 42,
            "title": "Fix auth",
            "state": "closed",
            "user": {"login": "alice"},
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-02T10:00:00Z",
            "merged_at": "2026-04-02T10:00:00Z",
            "closed_at": "2026-04-02T10:00:00Z",
            "draft": False,
            "additions": 50,
            "deletions": 10,
            "changed_files": 3,
            "labels": [{"name": "bug"}, {"name": "priority"}],
        }
        result = _slim_pr(raw)
        assert result["number"] == 42
        assert result["author"] == "alice"
        assert result["additions"] == 50
        assert result["labels"] == ["bug", "priority"]

    def test_slim_pr_handles_missing_user(self):
        from robothor.engine.tools.handlers.github_api import _slim_pr

        result = _slim_pr({"number": 1, "title": "Test", "state": "open"})
        assert result["author"] == ""


# ─── Pagination ─────────────────────────────────────────────────────


class TestPagination:
    @pytest.mark.asyncio
    async def test_follows_next_link(self):
        from robothor.engine.tools.handlers.github_api import _paginate

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.raise_for_status = lambda: None
        resp1.json.return_value = [{"id": 1}]
        resp1.headers = {"Link": '<https://api.github.com/next?page=2>; rel="next"'}

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.raise_for_status = lambda: None
        resp2.json.return_value = [{"id": 2}]
        resp2.headers = {}

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[resp1, resp2])

        results = await _paginate(client, "https://api.github.com/test", {}, {}, max_pages=3)
        assert len(results) == 2
        assert results[0]["id"] == 1
        assert results[1]["id"] == 2

    @pytest.mark.asyncio
    async def test_respects_max_pages(self):
        from robothor.engine.tools.handlers.github_api import _paginate

        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json.return_value = [{"id": 1}]
        resp.headers = {"Link": '<https://api.github.com/next?page=2>; rel="next"'}

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        results = await _paginate(client, "https://api.github.com/test", {}, {}, max_pages=1)
        assert len(results) == 1


# ─── Tool handlers ──────────────────────────────────────────────────

_ENV = {"GITHUB_TOKEN": "ghp_test123"}


class TestGithubListPrs:
    @pytest.mark.asyncio
    async def test_missing_token(self):
        from robothor.engine.tools.handlers.github_api import _github_list_prs

        with patch.dict("os.environ", {}, clear=True):
            result = await _github_list_prs({}, _CTX)
            assert "error" in result
            assert "GITHUB_TOKEN" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_repo(self):
        from robothor.engine.tools.handlers.github_api import _github_list_prs

        with patch.dict("os.environ", _ENV):
            result = await _github_list_prs({}, _CTX)
            assert result == {"error": "repo is required (format: owner/repo)"}

    @pytest.mark.asyncio
    async def test_list_prs_success(self):
        from robothor.engine.tools.handlers.github_api import _github_list_prs

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = [
            {
                "number": 1,
                "title": "Add feature",
                "state": "open",
                "user": {"login": "dev1"},
                "created_at": "2026-04-01T10:00:00Z",
                "updated_at": "2026-04-02T10:00:00Z",
                "merged_at": None,
                "closed_at": None,
                "draft": False,
                "additions": 100,
                "deletions": 20,
                "changed_files": 5,
                "labels": [],
            }
        ]
        mock_resp.headers = {}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _github_list_prs({"repo": "acme/test-repo"}, _CTX)

        assert result["count"] == 1
        assert result["repo"] == "acme/test-repo"
        assert result["pull_requests"][0]["author"] == "dev1"

    @pytest.mark.asyncio
    async def test_repo_not_found(self):
        from robothor.engine.tools.handlers.github_api import _github_list_prs

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
            )
        )
        mock_resp.headers = {}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _github_list_prs({"repo": "acme/nonexistent"}, _CTX)
            assert "not found" in result["error"].lower() or "404" in result["error"]


class TestGithubGetPr:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        from robothor.engine.tools.handlers.github_api import _github_get_pr

        with patch.dict("os.environ", _ENV):
            result = await _github_get_pr({}, _CTX)
            assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_get_pr_with_reviews(self):
        from robothor.engine.tools.handlers.github_api import _github_get_pr

        pr_resp = MagicMock()
        pr_resp.status_code = 200
        pr_resp.raise_for_status = lambda: None
        pr_resp.json.return_value = {
            "number": 10,
            "title": "Big change",
            "state": "closed",
            "user": {"login": "alice"},
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-03T10:00:00Z",
            "merged_at": "2026-04-03T10:00:00Z",
            "closed_at": "2026-04-03T10:00:00Z",
            "draft": False,
            "additions": 200,
            "deletions": 50,
            "changed_files": 10,
            "labels": [],
        }

        reviews_resp = MagicMock()
        reviews_resp.status_code = 200
        reviews_resp.raise_for_status = lambda: None
        reviews_resp.json.return_value = [
            {
                "user": {"login": "bob"},
                "state": "APPROVED",
                "submitted_at": "2026-04-02T10:00:00Z",
            }
        ]

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[pr_resp, reviews_resp])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _github_get_pr({"repo": "acme/test-repo", "pr_number": 10}, _CTX)

        assert result["number"] == 10
        assert len(result["reviews"]) == 1
        assert result["reviews"][0]["reviewer"] == "bob"
        assert result["hours_to_first_review"] == 24.0


def _mk_search_resp(items: list[dict], link: str = "") -> MagicMock:
    """Build a mocked /search/issues response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json.return_value = {"total_count": len(items), "items": items}
    resp.headers = {"Link": link}
    return resp


def _mk_pr_detail_resp(
    number: int,
    author: str,
    created_at: str,
    merged_at: str | None,
    *,
    merged_by: str = "alice",
    additions: int = 50,
    deletions: int = 10,
    draft: bool = False,
) -> MagicMock:
    """Build a mocked /repos/.../pulls/{n} response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "number": number,
        "title": f"PR {number}",
        "state": "closed",
        "user": {"login": author},
        "created_at": created_at,
        "updated_at": merged_at or created_at,
        "merged_at": merged_at,
        "closed_at": merged_at,
        "draft": draft,
        "additions": additions,
        "deletions": deletions,
        "merged_by": {"login": merged_by},
        "labels": [],
    }
    resp.headers = {}
    return resp


def _mock_async_client(responses: list[MagicMock]) -> AsyncMock:
    """Wrap a list of pre-built responses as an httpx.AsyncClient mock."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=responses)
    return client


class TestGithubPrStats:
    @pytest.mark.asyncio
    async def test_missing_repo(self):
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        with patch.dict("os.environ", _ENV):
            result = await _github_pr_stats({}, _CTX)
            assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_since(self):
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        with patch.dict("os.environ", _ENV):
            result = await _github_pr_stats({"repo": "acme/test"}, _CTX)
            assert "error" in result
            assert "since" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_merged_prs_in_window(self):
        """Search returns empty → no detail fetches, merged_count is 0."""
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        client = _mock_async_client([_mk_search_resp([])])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            result = await _github_pr_stats(
                {
                    "repo": "acme/test",
                    "since": "2026-05-12T04:00:00+00:00",
                    "until": "2026-05-19T04:00:00+00:00",
                },
                _CTX,
            )

        assert result["merged_count"] == 0
        # Only the search call should have happened — no /pulls/{n} fetches.
        assert client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_aggregates_humans_filters_bots(self):
        """Bots routed to bot_authors/bot_merged_count; humans aggregated normally."""
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        search = _mk_search_resp([{"number": 1}, {"number": 2}, {"number": 3}, {"number": 4}])
        details = [
            _mk_pr_detail_resp(
                1, "alice", "2026-05-13T10:00:00+00:00", "2026-05-14T10:00:00+00:00"
            ),
            _mk_pr_detail_resp(2, "bob", "2026-05-14T10:00:00+00:00", "2026-05-15T22:00:00+00:00"),
            _mk_pr_detail_resp(
                3,
                "dependabot[bot]",
                "2026-05-13T10:00:00+00:00",
                "2026-05-13T10:30:00+00:00",
            ),
            _mk_pr_detail_resp(
                4,
                "renovate[bot]",
                "2026-05-13T10:00:00+00:00",
                "2026-05-13T10:30:00+00:00",
            ),
        ]
        client = _mock_async_client([search, *details])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            result = await _github_pr_stats(
                {
                    "repo": "acme/test",
                    "since": "2026-05-12T04:00:00+00:00",
                    "until": "2026-05-19T04:00:00+00:00",
                },
                _CTX,
            )

        # Humans-only count
        assert result["merged_count"] == 2
        # Bots visible, not silently dropped
        assert result["bot_merged_count"] == 2
        assert result["bot_authors"]["dependabot[bot]"] == 1
        assert result["bot_authors"]["renovate[bot]"] == 1
        # Human author aggregation
        assert result["authors"] == {"alice": 1, "bob": 1}
        # Cycle time computed only from human, non-draft PRs (24h + 36h)
        assert result["avg_cycle_time_hours"] == 30.0

    @pytest.mark.asyncio
    async def test_drafts_excluded_from_cycle_time_but_counted(self):
        """Drafts inflate cycle time → exclude from cycle calc but include in merged_count."""
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        search = _mk_search_resp([{"number": 1}, {"number": 2}])
        details = [
            # Non-draft: 24h cycle
            _mk_pr_detail_resp(
                1, "alice", "2026-05-13T10:00:00+00:00", "2026-05-14T10:00:00+00:00"
            ),
            # Draft: 240h cycle — would inflate average if included
            _mk_pr_detail_resp(
                2,
                "bob",
                "2026-05-04T10:00:00+00:00",
                "2026-05-14T10:00:00+00:00",
                draft=True,
            ),
        ]
        client = _mock_async_client([search, *details])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            result = await _github_pr_stats(
                {
                    "repo": "acme/test",
                    "since": "2026-05-12T04:00:00+00:00",
                    "until": "2026-05-19T04:00:00+00:00",
                },
                _CTX,
            )

        # Both PRs counted in merged_count
        assert result["merged_count"] == 2
        # Cycle excludes draft → only the 24h PR contributes
        assert result["avg_cycle_time_hours"] == 24.0

    @pytest.mark.asyncio
    async def test_search_query_uses_range_with_z_suffix(self):
        """The Search API must use the `merged:A..B` range form with Z-suffixed timestamps.

        Regression: GitHub silently ignores `merged:>=A merged:<B` combined
        and the `+00:00` offset form. Production was returning ALL merged
        PRs for the repo instead of the date-windowed subset.
        """
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        search = _mk_search_resp([])
        client = _mock_async_client([search])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=client,
            ),
        ):
            await _github_pr_stats(
                {
                    "repo": "acme/test",
                    "since": "2026-05-12T04:00:00+00:00",
                    "until": "2026-05-19T04:00:00+00:00",
                },
                _CTX,
            )

        call_args = client.get.await_args_list[0]
        url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
        params = call_args.kwargs.get("params", {})
        assert "/search/issues" in url
        q = params.get("q", "")
        assert "repo:acme/test" in q
        assert "is:pr" in q
        assert "is:merged" in q
        # Range syntax with Z suffix; upper bound is until-1s for half-open
        assert "merged:2026-05-12T04:00:00Z..2026-05-19T03:59:59Z" in q
        # Make sure the buggy forms are NOT used
        assert "merged:>=" not in q
        assert "+00:00" not in q


class TestGithubCommitActivity:
    @pytest.mark.asyncio
    async def test_missing_repo(self):
        from robothor.engine.tools.handlers.github_api import _github_commit_activity

        with patch.dict("os.environ", _ENV):
            result = await _github_commit_activity({}, _CTX)
            assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_stats_computing_retry(self):
        from robothor.engine.tools.handlers.github_api import _github_commit_activity

        resp_202 = MagicMock()
        resp_202.status_code = 202

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.raise_for_status = lambda: None
        resp_200.json.return_value = [
            {
                "author": {"login": "dev1"},
                "total": 100,
                "weeks": [{"c": 5, "a": 100, "d": 20}],
            }
        ]

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[resp_202, resp_200])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.github_api.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await _github_commit_activity({"repo": "acme/test-repo"}, _CTX)

        assert result["contributors"][0]["author"] == "dev1"
        assert result["contributors"][0]["recent_commits"] == 5


class TestGithubReviewStats:
    @pytest.mark.asyncio
    async def test_missing_repo(self):
        from robothor.engine.tools.handlers.github_api import _github_review_stats

        with patch.dict("os.environ", _ENV):
            result = await _github_review_stats({}, _CTX)
            assert "required" in result["error"]
