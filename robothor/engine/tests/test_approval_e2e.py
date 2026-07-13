"""Integration coverage for the runner-facing half of the human-approval
escalation loop: guardrail `escalate` -> `PermissionEscalationManager.
request_approval` -> Telegram prompt -> `resolve` -> approved/denied.

`runner.py:1910-1984` calls `mgr.request_approval(...)` with this exact
argument shape when a guardrail's `check_pre_execution` returns
`action="escalate"` (see `robothor/engine/runner.py`). Driving the full
runner loop for this would require constructing an `AgentRunner`, a
`GuardrailEngine`, and a live session -- heavy scaffolding for a branch
that is otherwise a single, well-isolated call site. These tests instead
drive the manager directly with the same call shape and a fake bot that
captures the Telegram send, proving the loop guardrail -> prompt -> resolve
-> return-value actually works end to end (the seam the brief for this task
explicitly sanctions testing at).

The "no manager initialized: observe auto-approves, enforce denies" case is
NOT duplicated here -- it is already covered by
`test_failclosed_approval.py::TestFailClosedOnMissingManager`, which
exercises `fail_closed_on_missing_manager()` (the exact function the
runner's no-manager branch calls) across the mode matrix.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.permission_escalation import PermissionEscalationManager


def _make_bot(message_id: int = 555) -> MagicMock:
    """A bare bot double matching the raw send_message contract
    `_send_prompt` expects (as `test_human_approval.py` also uses)."""
    bot = MagicMock()
    sent = MagicMock()
    sent.message_id = message_id
    bot.send_message = AsyncMock(return_value=sent)
    return bot


async def _escalate(mgr: PermissionEscalationManager, **overrides: object) -> bool:
    """Call request_approval with the same argument shape runner.py's
    guardrail-escalate branch uses (runner.py:1919-1927)."""
    kwargs: dict[str, object] = {
        "agent_id": "test-agent",
        "run_id": "run-1",
        "tool_name": "exec_command",
        "tool_args": {"command": "rm -rf /tmp/scratch"},
        "guardrail_name": "destructive_write",
        "reason": "destructive command flagged for review",
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return await mgr.request_approval(**kwargs)  # type: ignore[arg-type]


class TestApprovalLoopEndToEnd:
    """Drives the real manager the way the runner's escalate branch does."""

    @pytest.mark.asyncio
    async def test_operator_approve_lets_request_approval_return_true(self) -> None:
        bot = _make_bot()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        task = asyncio.create_task(_escalate(mgr))
        await asyncio.sleep(0)  # let request_approval send the prompt and start waiting

        assert len(mgr._pending) == 1
        request_id = next(iter(mgr._pending))

        # Prove the prompt actually went out with the callback_data shape
        # on_permission_decision (telegram.py) parses.
        bot.send_message.assert_called_once()
        keyboard = bot.send_message.call_args.kwargs["reply_markup"]
        callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        assert callback_data == [
            f"perm:approve:{request_id}",
            f"perm:all:{request_id}",
            f"perm:deny:{request_id}",
        ]

        # Simulate the Telegram callback consumer's resolve() call.
        mgr.resolve(request_id, approved=True)

        assert await task is True

    @pytest.mark.asyncio
    async def test_operator_deny_lets_request_approval_return_false(self) -> None:
        bot = _make_bot()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        task = asyncio.create_task(_escalate(mgr))
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        mgr.resolve(request_id, approved=False)

        assert await task is False

    @pytest.mark.asyncio
    async def test_timeout_with_no_operator_response_denies(self) -> None:
        """Short-timeout override of the escalate branch's fail-secure path."""
        bot = _make_bot()
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")

        result = await _escalate(mgr, timeout_seconds=0.05)

        assert result is False
        bot.send_message.assert_called_once()  # the prompt was sent before the deny
