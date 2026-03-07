"""Tests for Google Workspace (gws CLI) tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

# ─── Tool registration ──────────────────────────────────────────────


class TestGwsToolSchemas:
    """Verify gws tools are registered in the tool registry."""

    def test_gws_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for tool_name in [
                "gws_gmail_search",
                "gws_gmail_get",
                "gws_gmail_send",
                "gws_gmail_modify",
                "gws_calendar_list",
                "gws_calendar_create",
                "gws_calendar_delete",
                "gws_chat_send",
            ]:
                assert tool_name in registry._schemas, f"{tool_name} not in registry"

    def test_gws_read_tools_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "gws_gmail_search" in READONLY_TOOLS
        assert "gws_gmail_get" in READONLY_TOOLS
        assert "gws_calendar_list" in READONLY_TOOLS

    def test_gws_write_tools_not_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "gws_gmail_send" not in READONLY_TOOLS
        assert "gws_gmail_modify" not in READONLY_TOOLS
        assert "gws_calendar_create" not in READONLY_TOOLS
        assert "gws_calendar_delete" not in READONLY_TOOLS
        assert "gws_chat_send" not in READONLY_TOOLS

    def test_gws_tools_in_set(self):
        from robothor.engine.tools import GWS_TOOLS

        assert len(GWS_TOOLS) == 8
        assert "gws_gmail_search" in GWS_TOOLS
        assert "gws_chat_send" in GWS_TOOLS


# ─── _run_gws helper ────────────────────────────────────────────────


class TestRunGws:
    def test_success_json(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"messages": [{"id": "abc"}]}'

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "messages" in result
            assert result["messages"][0]["id"] == "abc"

    def test_error_exit_code(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "auth failed"

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "error" in result
            assert "auth failed" in result["error"]

    def test_non_json_output(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "deleted successfully"

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["calendar", "events", "delete"])
            assert "output" in result
            assert "deleted" in result["output"]

    def test_timeout(self):
        import subprocess as sp

        with patch(
            "robothor.engine.tools.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="gws", timeout=30),
        ):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "error" in result
            assert "timed out" in result["error"]

    def test_file_not_found(self):
        with patch(
            "robothor.engine.tools.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "error" in result
            assert "not found" in result["error"]


# ─── Gmail search ────────────────────────────────────────────────────


class TestGwsGmailSearch:
    def test_search_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(
            {"messages": [{"id": "msg1", "threadId": "t1"}], "resultSizeEstimate": 1}
        )

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_gmail_search", {"query": "is:unread"})
            assert "messages" in result

            cmd = mock_run.call_args[0][0]
            assert cmd[:4] == ["gws", "gmail", "users", "messages"]
            params = json.loads(cmd[cmd.index("--params") + 1])
            assert params["q"] == "is:unread"
            assert params["userId"] == "me"

    def test_search_max_results_capped(self):
        mock_result = MagicMock(returncode=0, stdout='{"messages":[]}')

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool("gws_gmail_search", {"query": "test", "max_results": 500})
            params = json.loads(
                mock_run.call_args[0][0][mock_run.call_args[0][0].index("--params") + 1]
            )
            assert params["maxResults"] == 100


# ─── Gmail get ───────────────────────────────────────────────────────


class TestGwsGmailGet:
    def test_get_message(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"id": "msg1", "snippet": "hello"})

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_gmail_get", {"message_id": "msg1"})
            assert result["id"] == "msg1"

            cmd = mock_run.call_args[0][0]
            assert "messages" in cmd
            assert "get" in cmd

    def test_get_thread(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"id": "t1", "messages": []})

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_gmail_get", {"thread_id": "t1"})
            assert result["id"] == "t1"

            cmd = mock_run.call_args[0][0]
            assert "threads" in cmd

    def test_get_requires_id(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_gmail_get", {})
        assert "error" in result


# ─── Gmail send ──────────────────────────────────────────────────────


class TestGwsGmailSend:
    def test_send_email(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"id": "sent1", "threadId": "t1"})

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "Hello", "body": "Hi there"},
            )
            assert result["id"] == "sent1"

            cmd = mock_run.call_args[0][0]
            assert "send" in cmd
            # Check --json body contains raw
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert "raw" in body

    def test_send_reply(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"r1"}')

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool(
                "gws_gmail_send",
                {
                    "to": "alice@example.com",
                    "subject": "Re: Hello",
                    "body": "Reply",
                    "thread_id": "t1",
                },
            )
            cmd = mock_run.call_args[0][0]
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert body["threadId"] == "t1"


# ─── Gmail modify ───────────────────────────────────────────────────


class TestGwsGmailModify:
    def test_mark_read(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"msg1"}')

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_modify",
                {"message_id": "msg1", "remove_labels": ["UNREAD"]},
            )
            assert "error" not in result

            cmd = mock_run.call_args[0][0]
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert body["removeLabelIds"] == ["UNREAD"]

    def test_modify_requires_message_id(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_gmail_modify", {"remove_labels": ["UNREAD"]})
        assert "error" in result

    def test_modify_requires_labels(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_gmail_modify", {"message_id": "msg1"})
        assert "error" in result


# ─── Calendar list ───────────────────────────────────────────────────


class TestGwsCalendarList:
    def test_list_events(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"items": [{"summary": "Meeting", "id": "e1"}]})

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_calendar_list",
                {"time_min": "2026-03-06T00:00:00Z"},
            )
            assert "items" in result

            cmd = mock_run.call_args[0][0]
            params = json.loads(cmd[cmd.index("--params") + 1])
            assert params["singleEvents"] is True
            assert params["orderBy"] == "startTime"

    def test_list_requires_time_min(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_calendar_list", {})
        assert "error" in result


# ─── Calendar create ─────────────────────────────────────────────────


class TestGwsCalendarCreate:
    def test_create_event(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"e1","summary":"Lunch"}')

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_calendar_create",
                {
                    "summary": "Lunch",
                    "start": "2026-03-07T12:00:00-05:00",
                    "end": "2026-03-07T13:00:00-05:00",
                    "attendees": ["alice@example.com"],
                },
            )
            assert result["id"] == "e1"

            cmd = mock_run.call_args[0][0]
            assert "insert" in cmd
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert body["summary"] == "Lunch"
            assert body["attendees"] == [{"email": "alice@example.com"}]

    def test_create_requires_fields(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_calendar_create", {"summary": "Test"})
        assert "error" in result


# ─── Calendar delete ─────────────────────────────────────────────────


class TestGwsCalendarDelete:
    def test_delete_event(self):
        mock_result = MagicMock(returncode=0, stdout="")

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool(
                "gws_calendar_delete",
                {"event_id": "e1"},
            )
            cmd = mock_run.call_args[0][0]
            assert "delete" in cmd
            params = json.loads(cmd[cmd.index("--params") + 1])
            assert params["eventId"] == "e1"

    def test_delete_requires_event_id(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_calendar_delete", {})
        assert "error" in result


# ─── Chat send ───────────────────────────────────────────────────────


class TestGwsChatSend:
    def test_send_message(self):
        mock_result = MagicMock(returncode=0, stdout='{"name":"spaces/x/messages/1"}')

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_chat_send",
                {"space": "spaces/AAAA", "text": "Hello from Robothor"},
            )
            assert "name" in result

            cmd = mock_run.call_args[0][0]
            assert "create" in cmd
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert body["text"] == "Hello from Robothor"

    def test_send_requires_space_and_text(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_chat_send", {"space": "spaces/AAAA"})
        assert "error" in result
        result = _handle_gws_tool("gws_chat_send", {"text": "hi"})
        assert "error" in result


# ─── Unknown gws tool ───────────────────────────────────────────────


class TestGwsUnknownTool:
    def test_unknown_gws_tool(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_unknown_thing", {})
        assert "error" in result
        assert "Unknown gws tool" in result["error"]


# ─── build_for_agent inclusion ───────────────────────────────────────


class TestGwsBuildForAgent:
    def _make_config(self, tools_allowed=None, tools_denied=None):
        config = MagicMock()
        config.tools_allowed = tools_allowed or []
        config.tools_denied = tools_denied or []
        config.can_spawn_agents = False
        return config

    def test_gws_tools_included_when_allowed(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            config = self._make_config(
                tools_allowed=["gws_gmail_search", "gws_calendar_list"],
            )
            schemas = registry.build_for_agent(config)
            names = {s["function"]["name"] for s in schemas}
            assert "gws_gmail_search" in names
            assert "gws_calendar_list" in names
            assert "gws_gmail_send" not in names

    def test_gws_tools_excluded_when_not_allowed(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            config = self._make_config(tools_allowed=["read_file", "exec"])
            schemas = registry.build_for_agent(config)
            names = {s["function"]["name"] for s in schemas}
            assert "gws_gmail_search" not in names
