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


class TestGithubPrStats:
    @pytest.mark.asyncio
    async def test_missing_repo(self):
        from robothor.engine.tools.handlers.github_api import _github_pr_stats

        with patch.dict("os.environ", _ENV):
            result = await _github_pr_stats({}, _CTX)
            assert "required" in result["error"]


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
