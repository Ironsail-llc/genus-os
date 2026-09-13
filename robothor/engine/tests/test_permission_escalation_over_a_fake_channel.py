"""The same four escalation outcomes, over a `Channel` instead of a raw bot.

`test_approval_e2e.py` runs these scenarios against the Telegram-bound path and
is deliberately left untouched: the operator's three buttons and the ``perm:``
callback data are production UX, and the manager's whole job is that they keep
working. This file is the proof that the *other* half is not a declaration —
that a manager built over a channel actually asks, actually hears the answer,
and still denies when nobody says anything.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.permission_escalation import (
    APPROVE,
    APPROVE_ALL,
    DENY,
    ESCALATION_OPTIONS,
    PermissionEscalationManager,
)


class FakeChannel:
    """A channel that answers with whatever the test told it to."""

    name = "fake"
    inbound_router = None

    def __init__(self, answer: str | None = None, raises: BaseException | None = None) -> None:
        self._answer = answer
        self._raises = raises
        self.calls: list[dict] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {"channel": self.name}

    async def send(self, target, text, **kw):
        raise AssertionError("an escalation must be an ask, not a broadcast")

    async def ask(self, question, options=(), *, timeout=300.0, target=""):
        self.calls.append(
            {"question": question, "options": list(options), "timeout": timeout, "target": target}
        )
        if self._raises is not None:
            raise self._raises
        return self._answer

    async def resolve_identity(self, native_id):
        raise NotImplementedError


async def _escalate(mgr: PermissionEscalationManager, *, timeout: float = 5.0) -> bool:
    return await mgr.request_approval(
        agent_id="test-agent",
        run_id="run-1",
        tool_name="exec",
        tool_args={"command": "rm -rf build"},
        guardrail_name="destructive_write",
        reason="deletes a directory",
        timeout_seconds=timeout,
    )


class TestEscalationOverAChannel:
    @pytest.mark.asyncio
    async def test_approve_returns_true_and_the_prompt_names_the_tool(self):
        channel = FakeChannel(answer=APPROVE)
        mgr = PermissionEscalationManager(channel=channel, target="c1")

        assert await _escalate(mgr) is True

        asked = channel.calls[0]
        assert asked["target"] == "c1"
        assert asked["options"] == list(ESCALATION_OPTIONS)
        assert "exec" in asked["question"]
        assert "destructive_write" in asked["question"]
        # Nothing left waiting in RAM after the answer.
        assert mgr._pending == {}

    @pytest.mark.asyncio
    async def test_deny_returns_false(self):
        mgr = PermissionEscalationManager(channel=FakeChannel(answer=DENY), target="c1")
        assert await _escalate(mgr) is False
        assert mgr._session_grants == {}

    @pytest.mark.asyncio
    async def test_approve_all_grants_the_session_and_the_next_call_never_asks(self):
        channel = FakeChannel(answer=APPROVE_ALL)
        mgr = PermissionEscalationManager(channel=channel, target="c1")

        assert await _escalate(mgr) is True
        assert mgr._session_grants == {"test-agent:destructive_write": {"exec"}}

        # Second call takes the session-grant fast path: no second prompt.
        assert await _escalate(mgr) is True
        assert len(channel.calls) == 1

    @pytest.mark.asyncio
    async def test_no_answer_denies(self):
        """``ask`` returning None is "nobody answered", and that is a denial.

        Fail-secure is the whole contract: a tool a guardrail flagged must not
        run because the operator was asleep.
        """
        channel = FakeChannel(answer=None)
        mgr = PermissionEscalationManager(channel=channel, target="c1")
        assert await _escalate(mgr) is False
        assert mgr._pending == {}

    @pytest.mark.asyncio
    async def test_an_unrecognised_answer_denies(self):
        mgr = PermissionEscalationManager(channel=FakeChannel(answer="maybe?"), target="c1")
        assert await _escalate(mgr) is False

    @pytest.mark.asyncio
    async def test_a_channel_that_cannot_ask_denies_rather_than_raising(self):
        channel = FakeChannel(raises=NotImplementedError("a bus has nobody to ask"))
        mgr = PermissionEscalationManager(channel=channel, target="c1")
        assert await _escalate(mgr) is False

    @pytest.mark.asyncio
    async def test_a_channel_that_breaks_denies(self):
        channel = FakeChannel(raises=RuntimeError("transport is down"))
        mgr = PermissionEscalationManager(channel=channel, target="c1")
        assert await _escalate(mgr) is False

    @pytest.mark.asyncio
    async def test_a_manager_with_neither_bot_nor_channel_denies(self):
        mgr = PermissionEscalationManager()
        assert await _escalate(mgr) is False


class TestTheBotPathStillWins:
    """A manager given both is the production wiring, and it must not change."""

    @pytest.mark.asyncio
    async def test_the_bot_keyboard_is_used_and_the_channel_is_not_asked(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        channel = FakeChannel(answer=APPROVE)
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345", channel=channel, target="c1")

        task = asyncio.create_task(_escalate(mgr, timeout=5.0))
        await asyncio.sleep(0)

        request_id = next(iter(mgr._pending))
        keyboard = bot.send_message.call_args.kwargs["reply_markup"]
        callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        assert callback_data == [
            f"perm:approve:{request_id}",
            f"perm:all:{request_id}",
            f"perm:deny:{request_id}",
        ]
        assert channel.calls == []

        mgr.resolve(request_id, approved=True)
        assert await task is True

    @pytest.mark.asyncio
    async def test_a_bot_that_cannot_deliver_falls_through_to_the_channel(self):
        """Today an undeliverable prompt is an instant denial. With a second
        surface armed, "the operator could not be reached" is a claim worth
        testing before it is made."""
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("telegram is down"))
        channel = FakeChannel(answer=APPROVE)
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345", channel=channel, target="c1")

        assert await _escalate(mgr) is True
        assert len(channel.calls) == 1

    @pytest.mark.asyncio
    async def test_a_bot_that_cannot_deliver_and_no_channel_still_denies(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("telegram is down"))
        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")
        assert await _escalate(mgr) is False


class TestEscalationEmitsApprovalRequired:
    @pytest.mark.asyncio
    async def test_the_waiting_run_is_told_a_person_is_being_asked(self):
        from robothor.engine import run_status

        run_status.reset_status_sinks()
        seen: list[dict] = []

        async def sink(event):
            seen.append(event)

        run_status.register_status_sink("run-1", sink)
        try:
            mgr = PermissionEscalationManager(channel=FakeChannel(answer=APPROVE), target="c1")
            await _escalate(mgr)
        finally:
            run_status.reset_status_sinks()

        assert len(seen) == 1
        assert seen[0]["event"] == "approval_required"
        assert seen[0]["kind"] == "escalation"
        assert seen[0]["run_id"] == "run-1"
        assert seen[0]["tool"] == "exec"
        assert seen[0]["options"] == list(ESCALATION_OPTIONS)
        # The id is what the bridge's answer endpoint resolves against, so it
        # has to be there and has to be the request's own.
        assert isinstance(seen[0]["id"], str) and seen[0]["id"]

    @pytest.mark.asyncio
    async def test_a_session_grant_asks_nobody_and_announces_nothing(self):
        from robothor.engine import run_status

        run_status.reset_status_sinks()
        seen: list[dict] = []

        async def sink(event):
            seen.append(event)

        run_status.register_status_sink("run-1", sink)
        try:
            mgr = PermissionEscalationManager(channel=FakeChannel(answer=APPROVE_ALL), target="c1")
            await _escalate(mgr)
            seen.clear()
            await _escalate(mgr)
        finally:
            run_status.reset_status_sinks()

        assert seen == []


class TestResolveReportsWhetherItSettledAnything:
    def test_resolving_a_known_request_is_true(self):
        from robothor.engine.permission_escalation import EscalationRequest

        mgr = PermissionEscalationManager(channel=FakeChannel(), target="c1")
        mgr._pending["req-1"] = EscalationRequest(
            request_id="req-1",
            agent_id="a",
            run_id="r",
            tool_name="exec",
            tool_args={},
            guardrail_name="g",
            reason="",
            created_at=0.0,
        )
        assert mgr.resolve("req-1", approved=True) is True
        # A second tap changes nothing and says so.
        assert mgr.resolve("req-1", approved=False) is False

    def test_resolving_an_unknown_request_is_false(self):
        mgr = PermissionEscalationManager(channel=FakeChannel(), target="c1")
        assert mgr.resolve("nope", approved=True) is False
