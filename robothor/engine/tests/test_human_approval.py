"""Tests for human-in-the-loop approval (permission_escalation.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.permission_escalation import (
    PermissionEscalationManager,
    get_permission_manager,
    init_permission_manager,
)


class TestEscalationManagerInit:
    def test_init_attributes(self):
        """Manager should store bot and chat_id, start with empty pending/grants."""
        bot = MagicMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")
        assert mgr._bot is bot
        assert mgr._chat_id == "12345"
        assert mgr._pending == {}
        assert mgr._session_grants == {}


class TestResolve:
    def test_resolve_approve(self):
        """Resolving with approved=True should set the event and mark approved."""
        bot = MagicMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        # Manually create a pending request
        from robothor.engine.permission_escalation import EscalationRequest

        req = EscalationRequest(
            request_id="req-1",
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={"command": "ls"},
            guardrail_name="destructive_write",
            reason="Needs approval",
            created_at=0.0,
        )
        mgr._pending["req-1"] = req

        mgr.resolve("req-1", approved=True)
        assert req.approved is True
        assert req.result.is_set()

    def test_resolve_deny(self):
        """Resolving with approved=False should mark as denied."""
        bot = MagicMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        from robothor.engine.permission_escalation import EscalationRequest

        req = EscalationRequest(
            request_id="req-2",
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={},
            guardrail_name="destructive_write",
            reason="test",
            created_at=0.0,
        )
        mgr._pending["req-2"] = req

        mgr.resolve("req-2", approved=False)
        assert req.approved is False
        assert req.result.is_set()


class TestSessionGrants:
    def test_resolve_with_session_grant(self):
        """Resolving with remember_session=True should store a session grant."""
        bot = MagicMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        from robothor.engine.permission_escalation import EscalationRequest

        req = EscalationRequest(
            request_id="req-3",
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={},
            guardrail_name="destructive_write",
            reason="test",
            created_at=0.0,
        )
        mgr._pending["req-3"] = req

        mgr.resolve("req-3", approved=True, remember_session=True)
        assert req.approved is True
        grant_key = "test-agent:destructive_write"
        assert grant_key in mgr._session_grants
        assert "exec_command" in mgr._session_grants[grant_key]

    @pytest.mark.asyncio
    async def test_session_grant_skips_approval(self):
        """After granting a session grant, the same tool+guardrail should auto-approve."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        # Pre-populate a session grant
        mgr._session_grants["test-agent:destructive_write"] = {"exec_command"}

        result = await mgr.request_approval(
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={"command": "rm -rf /tmp/test"},
            guardrail_name="destructive_write",
            reason="Needs approval",
            timeout_seconds=1.0,
        )
        assert result is True
        # Bot should NOT have been called — fast-path skipped it
        bot.send_message.assert_not_called()


class TestCleanupExpired:
    def test_expired_requests_removed(self):
        """Expired requests should be removed and auto-approved."""
        bot = MagicMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        from robothor.engine.permission_escalation import EscalationRequest

        req = EscalationRequest(
            request_id="req-old",
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={},
            guardrail_name="destructive_write",
            reason="test",
            created_at=0.0,  # very old
        )
        mgr._pending["req-old"] = req

        removed = mgr.cleanup_expired(max_age=0)
        assert removed == 1
        assert "req-old" not in mgr._pending
        assert req.approved is True
        assert req.result.is_set()

    def test_non_expired_not_removed(self):
        """Requests within the age limit should not be removed."""
        import time

        bot = MagicMock()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        from robothor.engine.permission_escalation import EscalationRequest

        req = EscalationRequest(
            request_id="req-fresh",
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={},
            guardrail_name="destructive_write",
            reason="test",
            created_at=time.monotonic(),
        )
        mgr._pending["req-fresh"] = req

        removed = mgr.cleanup_expired(max_age=9999)
        assert removed == 0
        assert "req-fresh" in mgr._pending


class TestAutoApproveOnTimeout:
    @pytest.mark.asyncio
    async def test_timeout_auto_approves(self):
        """With a very short timeout, the request should auto-approve."""
        bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=mock_msg)
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        result = await mgr.request_approval(
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={"command": "ls"},
            guardrail_name="destructive_write",
            reason="Needs approval",
            timeout_seconds=0.01,
        )
        assert result is True


class TestSingleton:
    def test_init_and_get(self):
        """init_permission_manager should create a retrievable singleton."""
        bot = MagicMock()
        mgr = init_permission_manager(bot, "99999")
        assert get_permission_manager() is mgr
        assert mgr._chat_id == "99999"
