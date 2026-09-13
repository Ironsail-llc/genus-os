"""The sweep must not out-vote the budget the manifest declared.

``cleanup_expired`` had no caller until the watchdog got one, and it reaped on a
flat ``max_age=600``. ``human_approval_timeout`` is per-agent and validated to
``10..3600``, and ``docs/runbooks/approval-enforce.md`` promises the call
auto-denies after *that*. So an agent configured above 600 s was force-denied at
600–660 s with its keyboard still live in the operator's chat — and when the
operator then tapped Approve they were told "Approved" for a decision the agent
had never received, because ``on_permission_decision`` ignored ``resolve()``'s
return value.

Two properties, one incident: a control may not be stricter than the budget it
enforces, and a UI may not confirm a decision that did not land.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import daemon
from robothor.engine.permission_escalation import (
    PermissionEscalationManager,
    init_permission_manager,
)


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return bot


async def _escalate(mgr: PermissionEscalationManager, timeout: float) -> bool:
    return await mgr.request_approval(
        agent_id="agent-1",
        run_id="run-1",
        tool_name="exec",
        tool_args={"command": "ls"},
        guardrail_name="destructive_write",
        reason="needs a human",
        timeout_seconds=timeout,
    )


def _age(mgr: PermissionEscalationManager, seconds: float) -> None:
    """Pretend every pending request was raised ``seconds`` ago."""
    for request in mgr._pending.values():
        shift = seconds - (time.monotonic() - request.created_at)
        request.created_at -= shift
        if request.expires_at is not None:
            request.expires_at -= shift


class TestTheRequestsOwnDeadlineWins:
    @pytest.mark.asyncio
    async def test_a_1800s_request_survives_the_sweep_at_601s(self):
        mgr = init_permission_manager(_bot(), "12345")
        task = asyncio.create_task(_escalate(mgr, 1800.0))
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        _age(mgr, 601.0)
        with patch("robothor.engine.agent_questions.expire_overdue_questions", return_value=[]):
            swept = await daemon._sweep_stale_questions()

        assert swept["prompts"] == 0
        assert request_id in mgr._pending
        assert not mgr._pending[request_id].result.is_set()

        # And it still resolves the way the operator decides.
        mgr.resolve(request_id, approved=True)
        assert await task is True

    @pytest.mark.asyncio
    async def test_the_same_request_is_denied_at_1801s(self):
        mgr = init_permission_manager(_bot(), "12345")
        task = asyncio.create_task(_escalate(mgr, 1800.0))
        await asyncio.sleep(0)

        _age(mgr, 1801.0)
        with patch("robothor.engine.agent_questions.expire_overdue_questions", return_value=[]):
            swept = await daemon._sweep_stale_questions()

        assert swept["prompts"] == 1
        assert await task is False

    @pytest.mark.asyncio
    async def test_a_short_request_is_not_reaped_before_the_floor(self):
        """``max_age`` is a floor, never a ceiling. A 30 s request whose waiter
        has already denied itself is an orphan either way; reaping it early buys
        nothing and reaping it late costs nothing, so the conservative rule is
        the one that cannot surprise a longer budget."""
        mgr = PermissionEscalationManager(bot=_bot(), chat_id="12345")
        task = asyncio.create_task(_escalate(mgr, 30.0))
        await asyncio.sleep(0)

        _age(mgr, 120.0)
        assert mgr.cleanup_expired() == 0

        _age(mgr, 900.0)
        assert mgr.cleanup_expired() == 1
        assert await task is False

    def test_a_request_with_no_recorded_deadline_keeps_the_legacy_rule(self):
        """A directly-constructed ``EscalationRequest`` (the shape
        ``test_human_approval.py`` builds) records no deadline, and must still
        be reaped on ``max_age`` alone."""
        from robothor.engine.permission_escalation import EscalationRequest

        mgr = PermissionEscalationManager(bot=_bot(), chat_id="12345")
        request = EscalationRequest(
            request_id="req-legacy",
            agent_id="a",
            run_id="r",
            tool_name="exec",
            tool_args={},
            guardrail_name="g",
            reason="",
            created_at=time.monotonic() - 700.0,
        )
        assert request.expires_at is None
        mgr._pending["req-legacy"] = request

        assert mgr.cleanup_expired() == 1
        assert request.approved is False


class TestALateTapIsNotConfirmed:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from robothor.engine import permission_escalation as pe_mod

        pe_mod._escalation_manager = None
        yield
        pe_mod._escalation_manager = None

    @pytest.fixture
    def bot(self, engine_config):
        from robothor.engine.telegram import TelegramBot

        with (
            patch("robothor.engine.telegram.Bot") as mock_bot_cls,
            patch("robothor.engine.telegram.Dispatcher"),
        ):
            raw = MagicMock()
            raw.send_message = AsyncMock(return_value=MagicMock(message_id=42))
            mock_bot_cls.return_value = raw
            instance = TelegramBot(engine_config, MagicMock())
            instance.bot = raw
            yield instance

    @staticmethod
    def _handler(bot):
        for call in bot.dp.callback_query.return_value.call_args_list:
            if call.args[0].__name__ == "on_permission_decision":
                return call.args[0]
        raise AssertionError("on_permission_decision was not registered")

    @staticmethod
    def _callback(data: str):
        callback = MagicMock()
        callback.data = data
        callback.message = MagicMock()
        callback.message.chat.id = 12345
        callback.message.text = "Agent agent-1 requesting approval"
        callback.message.edit_reply_markup = AsyncMock()
        callback.message.edit_text = AsyncMock()
        callback.from_user = MagicMock(id=999)
        callback.answer = AsyncMock()
        return callback

    @pytest.mark.asyncio
    async def test_a_tap_on_a_swept_request_says_it_is_no_longer_pending(self, bot):
        mgr = init_permission_manager(bot, "12345")
        task = asyncio.create_task(_escalate(mgr, 60.0))
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        _age(mgr, 1200.0)
        assert mgr.cleanup_expired() == 1
        assert await task is False

        callback = self._callback(f"perm:approve:{request_id}")
        await self._handler(bot)(callback)

        said = " ".join(str(a) for a in callback.answer.call_args.args)
        assert "Approved" not in said
        assert "no longer pending" in said.lower()
        # The keyboard is retired and the message itself says so, because the
        # operator will scroll back to it and read it as an approval otherwise.
        callback.message.edit_text.assert_awaited_once()
        assert "no longer pending" in callback.message.edit_text.await_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_a_tap_that_lands_still_confirms(self, bot):
        mgr = init_permission_manager(bot, "12345")
        task = asyncio.create_task(_escalate(mgr, 60.0))
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        callback = self._callback(f"perm:approve:{request_id}")
        await self._handler(bot)(callback)

        assert await task is True
        callback.answer.assert_called_once_with("Approved")
        callback.message.edit_text.assert_not_awaited()
