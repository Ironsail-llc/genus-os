"""Tests for Google Chat gws tools (gws_chat_list_spaces, gws_chat_list_messages)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

# ─── Tool registration ──────────────────────────────────────────────


class TestChatToolSchemas:
    """Verify chat tools are registered in the tool registry."""

    def test_chat_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            assert "gws_chat_list_spaces" in registry._schemas
            assert "gws_chat_list_messages" in registry._schemas

    def test_chat_read_tools_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "gws_chat_list_spaces" in READONLY_TOOLS
        assert "gws_chat_list_messages" in READONLY_TOOLS

    def test_chat_tools_in_gws_set(self):
        from robothor.engine.tools import GWS_TOOLS

        assert "gws_chat_list_spaces" in GWS_TOOLS
        assert "gws_chat_list_messages" in GWS_TOOLS
        assert len(GWS_TOOLS) == 11

    def test_chat_send_not_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "gws_chat_send" not in READONLY_TOOLS


# ─── Handler tests ──────────────────────────────────────────────────


class TestChatListSpaces:
    def test_list_spaces_default(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(
            {
                "spaces": [
                    {"name": "spaces/AAAA", "displayName": "Team Chat", "spaceType": "ROOM"},
                ]
            }
        )

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_chat_list_spaces", {})
            assert "spaces" in result
            assert result["spaces"][0]["name"] == "spaces/AAAA"

            # Verify gws command structure
            call_args = mock_run.call_args[0][0]
            assert call_args[:4] == ["gws", "chat", "spaces", "list"]
            params = json.loads(call_args[call_args.index("--params") + 1])
            assert params["pageSize"] == 50

    def test_list_spaces_custom_page_size(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"spaces": []}'

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool("gws_chat_list_spaces", {"page_size": 10})
            call_args = mock_run.call_args[0][0]
            params = json.loads(call_args[call_args.index("--params") + 1])
            assert params["pageSize"] == 10

    def test_list_spaces_caps_page_size(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"spaces": []}'

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool("gws_chat_list_spaces", {"page_size": 5000})
            call_args = mock_run.call_args[0][0]
            params = json.loads(call_args[call_args.index("--params") + 1])
            assert params["pageSize"] == 1000


class TestChatListMessages:
    def test_list_messages_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(
            {
                "messages": [
                    {
                        "name": "spaces/AAAA/messages/BBBB",
                        "text": "Hello Robothor",
                        "sender": {"displayName": "Alice", "type": "HUMAN"},
                    }
                ]
            }
        )

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_chat_list_messages", {"space": "spaces/AAAA"})
            assert "messages" in result
            assert result["messages"][0]["text"] == "Hello Robothor"

            call_args = mock_run.call_args[0][0]
            params = json.loads(call_args[call_args.index("--params") + 1])
            assert params["parent"] == "spaces/AAAA"
            assert params["pageSize"] == 25

    def test_list_messages_missing_space(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_chat_list_messages", {})
        assert "error" in result
        assert "space is required" in result["error"]

    def test_list_messages_caps_page_size(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"messages": []}'

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool("gws_chat_list_messages", {"space": "spaces/X", "page_size": 500})
            call_args = mock_run.call_args[0][0]
            params = json.loads(call_args[call_args.index("--params") + 1])
            assert params["pageSize"] == 100

    def test_list_messages_custom_page_size(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"messages": []}'

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool("gws_chat_list_messages", {"space": "spaces/X", "page_size": 10})
            call_args = mock_run.call_args[0][0]
            params = json.loads(call_args[call_args.index("--params") + 1])
            assert params["pageSize"] == 10


class TestChatToolErrors:
    def test_gws_error_exit_code(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "auth failed"

        with patch("robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_chat_list_spaces", {})
            assert "error" in result

    def test_gws_timeout(self):
        import subprocess

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gws", timeout=30),
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_chat_list_messages", {"space": "spaces/X"})
            assert "error" in result
            assert "timed out" in result["error"]

    def test_gws_bad_json(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json at all"

        with patch("robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool("gws_chat_list_spaces", {})
            assert "output" in result


# ─── build_for_agent filtering ───────────────────────────────────────


class TestBuildForAgent:
    def test_chat_tools_included_when_allowed(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            agent_config = MagicMock()
            agent_config.tools_allowed = [
                "gws_chat_list_spaces",
                "gws_chat_list_messages",
                "gws_chat_send",
            ]
            agent_config.tools_denied = []

            schemas = registry.build_for_agent(agent_config)
            names = {s["function"]["name"] for s in schemas}
            assert "gws_chat_list_spaces" in names
            assert "gws_chat_list_messages" in names
            assert "gws_chat_send" in names

    def test_chat_tools_excluded_when_not_allowed(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            agent_config = MagicMock()
            agent_config.tools_allowed = ["read_file"]
            agent_config.tools_denied = []

            schemas = registry.build_for_agent(agent_config)
            names = {s["function"]["name"] for s in schemas}
            assert "gws_chat_list_spaces" not in names
            assert "gws_chat_list_messages" not in names
