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
                "gws_gmail_reply",
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

        assert "gws_gmail_reply" not in READONLY_TOOLS
        assert "gws_gmail_send" not in READONLY_TOOLS
        assert "gws_gmail_modify" not in READONLY_TOOLS
        assert "gws_calendar_create" not in READONLY_TOOLS
        assert "gws_calendar_delete" not in READONLY_TOOLS
        assert "gws_chat_send" not in READONLY_TOOLS

    def test_gws_tools_in_set(self):
        from robothor.engine.tools import GWS_TOOLS

        assert len(GWS_TOOLS) == 11
        assert "gws_gmail_search" in GWS_TOOLS
        assert "gws_chat_send" in GWS_TOOLS
        assert "gws_chat_list_spaces" in GWS_TOOLS
        assert "gws_chat_list_messages" in GWS_TOOLS


# ─── _run_gws helper ────────────────────────────────────────────────


class TestRunGws:
    def test_success_json(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"messages": [{"id": "abc"}]}'

        with patch("robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "messages" in result
            assert result["messages"][0]["id"] == "abc"

    def test_error_exit_code(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "auth failed"

        with patch("robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "error" in result
            assert "auth failed" in result["error"]

    def test_non_json_output(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "deleted successfully"

        with patch("robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["calendar", "events", "delete"])
            assert "output" in result
            assert "deleted" in result["output"]

    def test_timeout(self):
        import subprocess as sp

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="gws", timeout=30),
        ):
            from robothor.engine.tools import _run_gws

            result = _run_gws(["gmail", "users", "messages", "list"])
            assert "error" in result
            assert "timed out" in result["error"]

    def test_file_not_found(self):
        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with (
            patch(
                "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
            ) as mock_run,
            patch.dict("os.environ", {"ROBOTHOR_OWNER_EMAIL": "owner@example.com"}),
        ):
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
            assert body["attendees"] == [
                {"email": "alice@example.com"},
                {"email": "owner@example.com"},
            ]

    def test_create_includes_meet_by_default(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"e2","summary":"Sync"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool(
                "gws_calendar_create",
                {
                    "summary": "Sync",
                    "start": "2026-04-10T14:00:00-04:00",
                    "end": "2026-04-10T15:00:00-04:00",
                },
            )

            cmd = mock_run.call_args[0][0]
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert "conferenceData" in body
            assert (
                body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"]
                == "hangoutsMeet"
            )

            params_idx = cmd.index("--params")
            params = json.loads(cmd[params_idx + 1])
            assert params["conferenceDataVersion"] == 1

    def test_create_without_meet(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"e3","summary":"Quick"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool(
                "gws_calendar_create",
                {
                    "summary": "Quick",
                    "start": "2026-04-10T14:00:00-04:00",
                    "end": "2026-04-10T15:00:00-04:00",
                    "with_meet": False,
                },
            )

            cmd = mock_run.call_args[0][0]
            json_idx = cmd.index("--json")
            body = json.loads(cmd[json_idx + 1])
            assert "conferenceData" not in body

            params_idx = cmd.index("--params")
            params = json.loads(cmd[params_idx + 1])
            assert "conferenceDataVersion" not in params

    def test_create_requires_fields(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_calendar_create", {"summary": "Test"})
        assert "error" in result


# ─── Calendar delete ─────────────────────────────────────────────────


class TestGwsCalendarDelete:
    def test_delete_event(self):
        mock_result = MagicMock(returncode=0, stdout="")

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", return_value=mock_result
        ) as mock_run:
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


# ─── Gmail reply ────────────────────────────────────────────────────


class TestGwsGmailReply:
    """Tests for the gws_gmail_reply tool."""

    def _thread_response(self, last_from="alice@example.com", subject="Hello"):
        """Build a mock thread response with configurable last message."""
        return json.dumps(
            {
                "id": "t1",
                "messages": [
                    {
                        "id": "msg1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": f"Alice <{last_from}>"},
                                {"name": "To", "value": "robothor@ironsail.ai"},
                                {"name": "Subject", "value": subject},
                                {
                                    "name": "Message-ID",
                                    "value": "<CABx123@mail.gmail.com>",
                                },
                            ]
                        },
                    },
                ],
            }
        )

    def test_reply_threads_correctly(self):
        """Reply includes threadId, In-Reply-To, References, and reply-all recipients."""
        thread_result = MagicMock(returncode=0, stdout=self._thread_response())
        send_result = MagicMock(returncode=0, stdout='{"id":"r1","threadId":"t1"}')

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return thread_result
            return send_result

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", side_effect=side_effect
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Thanks, Alice!"},
            )
            assert result["id"] == "r1"

            # Verify the send call has threadId and proper headers
            send_cmd = mock_run.call_args_list[-1][0][0]
            json_idx = send_cmd.index("--json")
            body = json.loads(send_cmd[json_idx + 1])
            assert body["threadId"] == "t1"

            # Decode the raw MIME to verify headers
            import base64

            raw_bytes = base64.urlsafe_b64decode(body["raw"])
            raw_str = raw_bytes.decode("utf-8")
            assert "In-Reply-To: <CABx123@mail.gmail.com>" in raw_str
            assert "References: <CABx123@mail.gmail.com>" in raw_str
            assert "alice@example.com" in raw_str
            assert "Re: Hello" in raw_str

    def test_reply_requires_thread_id(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_gmail_reply", {"body": "Hello"})
        assert "error" in result
        assert "thread_id" in result["error"]

    def test_reply_requires_body(self):
        from robothor.engine.tools import _handle_gws_tool

        result = _handle_gws_tool("gws_gmail_reply", {"thread_id": "t1"})
        assert "error" in result
        assert "body" in result["error"]

    def test_reply_skips_duplicate(self):
        """Skip if last message is already from robothor."""
        thread_result = MagicMock(
            returncode=0,
            stdout=self._thread_response(last_from="robothor@ironsail.ai"),
        )

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=thread_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Duplicate reply"},
            )
            assert result["status"] == "skipped"
            assert "Already replied" in result["reason"]

    def test_reply_auto_prefixes_re(self):
        """Subject gets Re: prefix if not already present."""
        thread_result = MagicMock(
            returncode=0, stdout=self._thread_response(subject="Meeting tomorrow")
        )
        send_result = MagicMock(returncode=0, stdout='{"id":"r2"}')

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return thread_result if call_count == 1 else send_result

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", side_effect=side_effect
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "See you there"},
            )
            send_cmd = mock_run.call_args_list[-1][0][0]
            json_idx = send_cmd.index("--json")
            import base64

            raw_bytes = base64.urlsafe_b64decode(json.loads(send_cmd[json_idx + 1])["raw"])
            assert "Re: Meeting tomorrow" in raw_bytes.decode("utf-8")

    def test_reply_adds_extra_cc(self):
        """Extra CC addresses beyond thread participants are included."""
        thread_result = MagicMock(returncode=0, stdout=self._thread_response())
        send_result = MagicMock(returncode=0, stdout='{"id":"r3"}')

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return thread_result if call_count == 1 else send_result

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run", side_effect=side_effect
        ) as mock_run:
            from robothor.engine.tools import _handle_gws_tool

            _handle_gws_tool(
                "gws_gmail_reply",
                {
                    "thread_id": "t1",
                    "body": "Looping in Philip",
                    "cc": "philip@ironsail.ai",
                },
            )
            send_cmd = mock_run.call_args_list[-1][0][0]
            json_idx = send_cmd.index("--json")
            import base64

            raw_bytes = base64.urlsafe_b64decode(json.loads(send_cmd[json_idx + 1])["raw"])
            raw_str = raw_bytes.decode("utf-8")
            assert "philip@ironsail.ai" in raw_str

    def test_reply_propagates_thread_fetch_error(self):
        """If thread fetch fails, error is returned."""
        error_result = MagicMock(returncode=1, stderr="Not found")

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=error_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "bad_id", "body": "Hello"},
            )
            assert "error" in result

    def test_reply_case_insensitive_duplicate_guard(self):
        """Duplicate guard works even when From header has different casing."""
        thread_json = self._thread_response(last_from="Robothor@Ironsail.AI")
        thread_result = MagicMock(returncode=0, stdout=thread_json)

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=thread_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Reply"},
            )
            assert result.get("status") == "skipped"

    def test_reply_missing_message_id(self):
        """Reply still sends when last message has no Message-ID header."""
        thread_json = json.dumps(
            {
                "id": "t1",
                "messages": [
                    {
                        "id": "msg1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "alice@example.com"},
                                {"name": "To", "value": "robothor@ironsail.ai"},
                                {"name": "Subject", "value": "Hello"},
                            ]
                        },
                    },
                ],
            }
        )
        thread_result = MagicMock(returncode=0, stdout=thread_json)
        send_result = MagicMock(returncode=0, stdout='{"id":"s1","threadId":"t1"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=[thread_result, send_result],
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Reply"},
            )
            assert "error" not in result

    def test_reply_no_recipients_after_filtering(self):
        """Returns error when only participant is robothor."""
        only_robothor = json.dumps(
            {
                "id": "t1",
                "messages": [
                    {
                        "id": "msg1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "someone@example.com"},
                                {"name": "To", "value": "robothor@ironsail.ai"},
                                {"name": "Subject", "value": "Hello"},
                                {"name": "Message-ID", "value": "<m1@mail>"},
                            ]
                        },
                    },
                    {
                        "id": "msg2",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "robothor@ironsail.ai"},
                                {"name": "To", "value": "someone@example.com"},
                                {"name": "Subject", "value": "Re: Hello"},
                                {"name": "Message-ID", "value": "<m2@mail>"},
                            ]
                        },
                    },
                ],
            }
        )
        # Last message is from robothor, so duplicate guard triggers
        thread_result = MagicMock(returncode=0, stdout=only_robothor)
        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=thread_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Reply"},
            )
            assert result.get("status") == "skipped"

    def test_reply_case_insensitive_address_filtering(self):
        """Mixed-case robothor address in To header is filtered from recipients."""
        thread_json = json.dumps(
            {
                "id": "t1",
                "messages": [
                    {
                        "id": "msg1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "alice@example.com"},
                                {"name": "To", "value": "Robothor@Ironsail.AI, bob@example.com"},
                                {"name": "Subject", "value": "Hello"},
                                {"name": "Message-ID", "value": "<m1@mail>"},
                            ]
                        },
                    },
                ],
            }
        )
        thread_result = MagicMock(returncode=0, stdout=thread_json)
        send_result = MagicMock(returncode=0, stdout='{"id":"s1","threadId":"t1"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=[thread_result, send_result],
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Reply"},
            )
            assert "error" not in result
            # Decode the sent MIME to verify robothor is not in recipients
            import base64

            # Check the subprocess call — second call is the send
            from robothor.engine.tools.handlers.gws import subprocess

            send_call = subprocess.run.call_args_list[1]
            json_arg = send_call[0][0][-1]  # last arg is the JSON body
            raw = json.loads(json_arg)["raw"]
            mime_bytes = base64.urlsafe_b64decode(raw)
            mime_text = mime_bytes.decode("utf-8")
            assert "robothor@ironsail.ai" not in mime_text.lower().split("to:")[1].split("\n")[0]

    def test_reply_multi_message_thread(self):
        """Reply-all collects addresses from all messages in the thread."""
        thread_json = json.dumps(
            {
                "id": "t1",
                "messages": [
                    {
                        "id": "msg1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "alice@example.com"},
                                {"name": "To", "value": "robothor@ironsail.ai"},
                                {"name": "Subject", "value": "Hello"},
                                {"name": "Message-ID", "value": "<m1@mail>"},
                            ]
                        },
                    },
                    {
                        "id": "msg2",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "robothor@ironsail.ai"},
                                {"name": "To", "value": "alice@example.com"},
                                {"name": "Cc", "value": "bob@example.com"},
                                {"name": "Subject", "value": "Re: Hello"},
                                {"name": "Message-ID", "value": "<m2@mail>"},
                            ]
                        },
                    },
                    {
                        "id": "msg3",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "charlie@example.com"},
                                {"name": "To", "value": "robothor@ironsail.ai, alice@example.com"},
                                {"name": "Subject", "value": "Re: Hello"},
                                {"name": "Message-ID", "value": "<m3@mail>"},
                            ]
                        },
                    },
                ],
            }
        )
        thread_result = MagicMock(returncode=0, stdout=thread_json)
        send_result = MagicMock(returncode=0, stdout='{"id":"s1","threadId":"t1"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=[thread_result, send_result],
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_reply",
                {"thread_id": "t1", "body": "Reply to all"},
            )
            assert "error" not in result
            # Verify all 3 external addresses are collected
            import base64

            from robothor.engine.tools.handlers.gws import subprocess

            send_call = subprocess.run.call_args_list[1]
            json_arg = send_call[0][0][-1]
            raw = json.loads(json_arg)["raw"]
            mime_text = base64.urlsafe_b64decode(raw).decode("utf-8")
            assert "alice@example.com" in mime_text
            assert "bob@example.com" in mime_text
            assert "charlie@example.com" in mime_text
            assert "robothor@ironsail.ai" not in mime_text.split("To:")[1].split("\n")[0]


class TestGwsGmailSendWarning:
    """Test the Re: subject warning when thread_id is missing."""

    def test_send_warns_on_reply_without_thread_id(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"s1","threadId":"t1"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=mock_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "Re: Hello", "body": "Reply"},
            )
            assert "_warning" in result
            assert "thread_id" in result["_warning"]

    def test_send_no_warning_for_new_email(self):
        mock_result = MagicMock(returncode=0, stdout='{"id":"s2","threadId":"t2"}')

        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=mock_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {"to": "alice@example.com", "subject": "Hello", "body": "New email"},
            )
            assert "_warning" not in result


# ─── gws_gmail_send duplicate guard ─────────────────────────────────


class TestGwsGmailSendDuplicateGuard:
    """Test the duplicate-reply guard in gws_gmail_send."""

    def _thread_response(self, last_from="robothor@ironsail.ai"):
        return json.dumps(
            {
                "id": "t1",
                "messages": [
                    {
                        "id": "msg1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": f"<{last_from}>"},
                            ]
                        },
                    },
                ],
            }
        )

    def test_send_skips_when_last_message_from_robothor(self):
        """If the last message is from robothor, the send is skipped."""
        thread_result = MagicMock(returncode=0, stdout=self._thread_response())
        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=thread_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {
                    "to": "alice@example.com",
                    "subject": "Re: Hello",
                    "body": "Reply",
                    "thread_id": "t1",
                },
            )
            assert result.get("status") == "skipped"

    def test_guard_exception_does_not_block_send(self):
        """If the guard throws, the send still proceeds."""
        # First call (thread fetch) raises, second call (send) succeeds
        send_result = MagicMock(returncode=0, stdout='{"id":"s1","threadId":"t1"}')
        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=[Exception("guard boom"), send_result],
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {
                    "to": "alice@example.com",
                    "subject": "Re: Hello",
                    "body": "Reply",
                    "thread_id": "t1",
                },
            )
            assert "error" not in result
            assert result.get("id") == "s1"

    def test_send_proceeds_when_last_message_from_other(self):
        """If the last message is from someone else, the send proceeds."""
        thread_result = MagicMock(
            returncode=0, stdout=self._thread_response(last_from="alice@example.com")
        )
        send_result = MagicMock(returncode=0, stdout='{"id":"s2","threadId":"t1"}')
        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            side_effect=[thread_result, send_result],
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {
                    "to": "alice@example.com",
                    "subject": "Re: Hello",
                    "body": "Reply",
                    "thread_id": "t1",
                },
            )
            assert result.get("id") == "s2"

    def test_guard_case_insensitive(self):
        """Guard catches mixed-case robothor email in From header."""
        thread_result = MagicMock(
            returncode=0,
            stdout=self._thread_response(last_from="Robothor@Ironsail.AI"),
        )
        with patch(
            "robothor.engine.tools.handlers.gws.subprocess.run",
            return_value=thread_result,
        ):
            from robothor.engine.tools import _handle_gws_tool

            result = _handle_gws_tool(
                "gws_gmail_send",
                {
                    "to": "alice@example.com",
                    "subject": "Re: Hello",
                    "body": "Reply",
                    "thread_id": "t1",
                },
            )
            assert result.get("status") == "skipped"


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
