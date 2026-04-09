"""Tests for JIRA Cloud API tool handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")

# ─── Tool registration ──────────────────────────────────────────────


class TestJiraToolSchemas:
    """Verify JIRA tools are registered in the tool registry."""

    def test_jira_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for tool_name in [
                "jira_search",
                "jira_get_issue",
                "jira_get_sprint",
                "jira_get_board_velocity",
                "jira_list_boards",
            ]:
                assert tool_name in registry._schemas, f"{tool_name} not in registry"

    def test_jira_tools_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        for tool_name in [
            "jira_search",
            "jira_get_issue",
            "jira_get_sprint",
            "jira_get_board_velocity",
            "jira_list_boards",
        ]:
            assert tool_name in READONLY_TOOLS, f"{tool_name} not in READONLY_TOOLS"

    def test_jira_tools_in_set(self):
        from robothor.engine.tools import JIRA_TOOLS

        assert len(JIRA_TOOLS) == 5
        assert "jira_search" in JIRA_TOOLS
        assert "jira_list_boards" in JIRA_TOOLS


# ─── Auth helpers ───────────────────────────────────────────────────


class TestJiraAuth:
    def test_missing_credentials(self):
        from robothor.engine.tools.handlers.jira import _get_auth_header, _get_base_url

        with patch.dict("os.environ", {}, clear=True):
            assert _get_base_url() == ""
            assert _get_auth_header() == ""

    def test_valid_credentials(self):
        from robothor.engine.tools.handlers.jira import _get_auth_header, _get_base_url

        with patch.dict(
            "os.environ",
            {
                "JIRA_BASE_URL": "https://test.atlassian.net",
                "JIRA_USER_EMAIL": "user@example.com",
                "JIRA_API_TOKEN": "token123",
            },
        ):
            assert _get_base_url() == "https://test.atlassian.net"
            assert _get_auth_header() != ""

    def test_base_url_strips_trailing_slash(self):
        from robothor.engine.tools.handlers.jira import _get_base_url

        with patch.dict("os.environ", {"JIRA_BASE_URL": "https://test.atlassian.net/"}):
            assert _get_base_url() == "https://test.atlassian.net"


# ─── Response slimming ──────────────────────────────────────────────


class TestSlimIssue:
    def test_slim_issue_extracts_fields(self):
        from robothor.engine.tools.handlers.jira import _slim_issue

        raw = {
            "key": "ENG-123",
            "fields": {
                "summary": "Fix the thing",
                "status": {
                    "name": "In Progress",
                    "statusCategory": {"name": "In Progress"},
                },
                "assignee": {
                    "displayName": "Alice",
                    "emailAddress": "alice@example.com",
                },
                "issuetype": {"name": "Story"},
                "priority": {"name": "High"},
                "customfield_10016": 5,
                "created": "2026-04-01T10:00:00.000+0000",
                "updated": "2026-04-02T10:00:00.000+0000",
                "resolutiondate": None,
                "labels": ["backend"],
            },
        }
        result = _slim_issue(raw)
        assert result["key"] == "ENG-123"
        assert result["summary"] == "Fix the thing"
        assert result["status"] == "In Progress"
        assert result["assignee"] == "Alice"
        assert result["story_points"] == 5
        assert result["labels"] == ["backend"]

    def test_slim_issue_handles_missing_fields(self):
        from robothor.engine.tools.handlers.jira import _slim_issue

        result = _slim_issue({"key": "X-1", "fields": {}})
        assert result["key"] == "X-1"
        assert result["assignee"] == "Unassigned"
        assert result["story_points"] is None


# ─── Cycle time extraction ──────────────────────────────────────────


class TestCycleTime:
    def test_extract_transitions(self):
        from robothor.engine.tools.handlers.jira import _extract_cycle_time

        issue = {
            "changelog": {
                "histories": [
                    {
                        "created": "2026-04-01T10:00:00.000+0000",
                        "items": [
                            {
                                "field": "status",
                                "fromString": "To Do",
                                "toString": "In Progress",
                            }
                        ],
                    },
                    {
                        "created": "2026-04-03T10:00:00.000+0000",
                        "items": [
                            {
                                "field": "status",
                                "fromString": "In Progress",
                                "toString": "Done",
                            },
                            {
                                "field": "assignee",
                                "fromString": "Alice",
                                "toString": "Bob",
                            },
                        ],
                    },
                ]
            }
        }
        transitions = _extract_cycle_time(issue)
        assert len(transitions) == 2
        assert transitions[0]["from_status"] == "To Do"
        assert transitions[0]["to_status"] == "In Progress"
        assert transitions[1]["from_status"] == "In Progress"
        assert transitions[1]["to_status"] == "Done"

    def test_extract_empty_changelog(self):
        from robothor.engine.tools.handlers.jira import _extract_cycle_time

        assert _extract_cycle_time({}) == []
        assert _extract_cycle_time({"changelog": {}}) == []


# ─── Tool handlers ──────────────────────────────────────────────────


_ENV = {
    "JIRA_BASE_URL": "https://test.atlassian.net",
    "JIRA_USER_EMAIL": "user@example.com",
    "JIRA_API_TOKEN": "token123",
}


class TestJiraSearch:
    @pytest.mark.asyncio
    async def test_missing_creds(self):
        from robothor.engine.tools.handlers.jira import _jira_search

        with patch.dict("os.environ", {}, clear=True):
            result = await _jira_search({}, _CTX)
            assert "error" in result
            assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_jql(self):
        from robothor.engine.tools.handlers.jira import _jira_search

        with patch.dict("os.environ", _ENV):
            result = await _jira_search({}, _CTX)
            assert result == {"error": "jql is required"}

    @pytest.mark.asyncio
    async def test_search_success(self):
        from robothor.engine.tools.handlers.jira import _jira_search

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "total": 1,
            "issues": [
                {
                    "key": "ENG-1",
                    "fields": {
                        "summary": "Test",
                        "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
                        "assignee": {"displayName": "Bob", "emailAddress": "bob@example.com"},
                        "issuetype": {"name": "Task"},
                        "priority": {"name": "Medium"},
                        "created": "2026-04-01",
                        "updated": "2026-04-02",
                        "resolutiondate": None,
                        "labels": [],
                    },
                }
            ],
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_search({"jql": "project = ENG"}, _CTX)

        assert result["count"] == 1
        assert result["issues"][0]["key"] == "ENG-1"

    @pytest.mark.asyncio
    async def test_search_invalid_jql(self):
        from robothor.engine.tools.handlers.jira import _jira_search

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Invalid JQL"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_search({"jql": "bad query"}, _CTX)
            assert "Invalid JQL" in result["error"]

    @pytest.mark.asyncio
    async def test_search_auth_failure(self):
        from robothor.engine.tools.handlers.jira import _jira_search

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_search({"jql": "project = ENG"}, _CTX)
            assert "authentication failed" in result["error"]


class TestJiraGetIssue:
    @pytest.mark.asyncio
    async def test_missing_issue_key(self):
        from robothor.engine.tools.handlers.jira import _jira_get_issue

        with patch.dict("os.environ", _ENV):
            result = await _jira_get_issue({}, _CTX)
            assert result == {"error": "issue_key is required"}

    @pytest.mark.asyncio
    async def test_issue_not_found(self):
        from robothor.engine.tools.handlers.jira import _jira_get_issue

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_get_issue({"issue_key": "NOPE-999"}, _CTX)
            assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_issue_with_changelog(self):
        from robothor.engine.tools.handlers.jira import _jira_get_issue

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {
            "key": "ENG-42",
            "fields": {
                "summary": "Implement feature",
                "status": {"name": "Done", "statusCategory": {"name": "Done"}},
                "assignee": {"displayName": "Alice", "emailAddress": "a@example.com"},
                "issuetype": {"name": "Story"},
                "priority": {"name": "High"},
                "created": "2026-04-01",
                "updated": "2026-04-03",
                "resolutiondate": "2026-04-03",
                "labels": [],
            },
            "changelog": {
                "histories": [
                    {
                        "created": "2026-04-02",
                        "items": [{"field": "status", "fromString": "To Do", "toString": "Done"}],
                    }
                ]
            },
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_get_issue({"issue_key": "ENG-42"}, _CTX)

        assert result["key"] == "ENG-42"
        assert len(result["transitions"]) == 1
        assert result["transitions"][0]["to_status"] == "Done"


class TestJiraListBoards:
    @pytest.mark.asyncio
    async def test_list_boards_success(self):
        from robothor.engine.tools.handlers.jira import _jira_list_boards

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = {
            "values": [
                {
                    "id": 1,
                    "name": "Engineering",
                    "type": "scrum",
                    "location": {"projectKey": "ENG", "projectName": "Engineering"},
                }
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_list_boards({}, _CTX)

        assert result["count"] == 1
        assert result["boards"][0]["name"] == "Engineering"
        assert result["boards"][0]["project_key"] == "ENG"


class TestJiraGetSprint:
    @pytest.mark.asyncio
    async def test_missing_board_id(self):
        from robothor.engine.tools.handlers.jira import _jira_get_sprint

        with patch.dict("os.environ", _ENV):
            result = await _jira_get_sprint({}, _CTX)
            assert result == {"error": "board_id is required"}

    @pytest.mark.asyncio
    async def test_completion_rate_calculation(self):
        from robothor.engine.tools.handlers.jira import _jira_get_sprint

        sprint_resp = MagicMock()
        sprint_resp.status_code = 200
        sprint_resp.raise_for_status = lambda: None
        sprint_resp.json.return_value = {
            "values": [
                {
                    "id": 10,
                    "name": "Sprint 5",
                    "state": "active",
                    "startDate": "2026-04-01",
                    "endDate": "2026-04-14",
                    "goal": "Ship v2",
                }
            ]
        }

        issues_resp = MagicMock()
        issues_resp.status_code = 200
        issues_resp.raise_for_status = lambda: None
        issues_resp.json.return_value = {
            "issues": [
                {
                    "key": "ENG-1",
                    "fields": {
                        "summary": "Done task",
                        "status": {"name": "Done", "statusCategory": {"name": "Done"}},
                        "assignee": None,
                        "issuetype": {"name": "Story"},
                        "priority": {"name": "Medium"},
                        "customfield_10016": 3,
                        "created": "",
                        "updated": "",
                        "resolutiondate": "",
                        "labels": [],
                    },
                },
                {
                    "key": "ENG-2",
                    "fields": {
                        "summary": "WIP task",
                        "status": {
                            "name": "In Progress",
                            "statusCategory": {"name": "In Progress"},
                        },
                        "assignee": None,
                        "issuetype": {"name": "Story"},
                        "priority": {"name": "Medium"},
                        "customfield_10016": 2,
                        "created": "",
                        "updated": "",
                        "resolutiondate": "",
                        "labels": [],
                    },
                },
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[sprint_resp, issues_resp])

        with (
            patch.dict("os.environ", _ENV),
            patch(
                "robothor.engine.tools.handlers.jira.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await _jira_get_sprint({"board_id": 1}, _CTX)

        assert result["total_points"] == 5
        assert result["completed_points"] == 3
        assert result["completion_rate"] == 60.0
        assert result["sprint"]["name"] == "Sprint 5"


class TestJiraVelocity:
    @pytest.mark.asyncio
    async def test_missing_board_id(self):
        from robothor.engine.tools.handlers.jira import _jira_get_board_velocity

        with patch.dict("os.environ", _ENV):
            result = await _jira_get_board_velocity({}, _CTX)
            assert result == {"error": "board_id is required"}
