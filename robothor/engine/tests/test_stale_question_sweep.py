"""The two expiry sweeps, and proof that something calls them.

``PermissionEscalationManager.cleanup_expired`` has existed since the manager
was written and nothing in the engine has ever called it: a prompt whose waiter
was cancelled (a run killed by the watchdog, a daemon reload) stayed in
``_pending`` for the life of the process, and a stale button kept resolving a
request nobody was waiting on. ``agent_questions`` has the same shape of hole on
the durable side — a pending row nobody expires is a backlog entry that never
stops being "open".

The interesting assertion in this file is not that either function works. It is
that the watchdog tick reaches them.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import daemon
from robothor.engine.permission_escalation import (
    EscalationRequest,
    PermissionEscalationManager,
    init_permission_manager,
)


def _stale_request(age: float) -> EscalationRequest:
    return EscalationRequest(
        request_id="req-old",
        agent_id="a",
        run_id="r",
        tool_name="exec",
        tool_args={},
        guardrail_name="g",
        reason="",
        created_at=time.monotonic() - age,
    )


class TestSweep:
    @pytest.mark.asyncio
    async def test_the_sweep_expires_stale_prompts_and_stale_rows(self):
        mgr = init_permission_manager(MagicMock(), "12345")
        mgr._pending["req-old"] = _stale_request(age=1200.0)

        with patch(
            "robothor.engine.agent_questions.expire_overdue_questions", return_value=[1, 2]
        ) as expire:
            swept = await daemon._sweep_stale_questions()

        assert swept == {"prompts": 1, "questions": 2}
        assert "req-old" not in mgr._pending
        expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_fresh_prompt_is_left_alone(self):
        mgr = init_permission_manager(MagicMock(), "12345")
        mgr._pending["req-new"] = _stale_request(age=1.0)
        mgr._pending["req-new"].request_id = "req-new"

        with patch("robothor.engine.agent_questions.expire_overdue_questions", return_value=[]):
            swept = await daemon._sweep_stale_questions()

        assert swept["prompts"] == 0
        assert "req-new" in mgr._pending

    @pytest.mark.asyncio
    async def test_no_manager_is_not_an_error(self):
        import robothor.engine.permission_escalation as pe

        previous = pe._escalation_manager
        pe._escalation_manager = None
        try:
            with patch("robothor.engine.agent_questions.expire_overdue_questions", return_value=[]):
                assert await daemon._sweep_stale_questions() == {"prompts": 0, "questions": 0}
        finally:
            pe._escalation_manager = previous

    @pytest.mark.asyncio
    async def test_a_database_that_is_down_does_not_take_the_watchdog_with_it(self):
        init_permission_manager(MagicMock(), "12345")
        with patch(
            "robothor.engine.agent_questions.expire_overdue_questions",
            side_effect=RuntimeError("db is gone"),
        ):
            assert await daemon._sweep_stale_questions() == {"prompts": 0, "questions": 0}

    def test_an_expired_prompt_denies_rather_than_vanishing(self):
        """A waiter still parked on the event must be woken, and woken denied."""
        mgr = PermissionEscalationManager(bot=MagicMock(), chat_id="12345")
        request = _stale_request(age=1200.0)
        mgr._pending["req-old"] = request

        assert mgr.cleanup_expired(max_age=600.0) == 1
        assert request.approved is False
        assert request.result.is_set()


class TestTheWatchdogCallsIt:
    def test_the_watchdog_tick_sweeps(self):
        """Grepped from source: the tick is a 200-line loop inside a daemon
        that needs a scheduler, a runner and a workflow engine to enter, and
        the claim being made is only that the call site exists — which is
        exactly the claim a green test of `_sweep_stale_questions` alone would
        not support."""
        from pathlib import Path

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        assert "await _sweep_stale_questions()" in src


class TestTheManagerHasASecondSurface:
    def test_a_bot_wired_manager_resolves_the_telegram_channel_lazily(self):
        """Resolved on first need, not at construction: ``daemon.main`` builds
        this manager ~70 lines before ``warm_channels()`` runs, so a channel
        captured at init would be whatever the registry held before warm-up."""
        mgr = init_permission_manager(MagicMock(), "12345")
        assert mgr._channel is None

        resolved = mgr._resolve_channel()
        assert resolved is not None
        assert getattr(resolved, "name", "") == "telegram"
        assert mgr._target == "12345"

    def test_a_manager_with_no_bot_and_no_channel_resolves_nothing(self):
        """Never guess a surface. Denying is the fail-secure answer."""
        assert PermissionEscalationManager()._resolve_channel() is None

    def test_an_explicit_channel_is_never_overridden(self):
        sentinel = MagicMock()
        mgr = PermissionEscalationManager(bot=MagicMock(), chat_id="1", channel=sentinel)
        assert mgr._resolve_channel() is sentinel
