"""Tests for delivery module — unexpanded env var guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from robothor.engine.delivery import _deliver_telegram, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus


@pytest.fixture(autouse=True)
def _register_mock_sender():
    """Register a mock Telegram sender for all tests."""
    sender = AsyncMock()
    set_telegram_sender(sender)
    yield sender
    set_telegram_sender(None)  # type: ignore[arg-type]


def _make_run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "id": "run-1",
        "agent_id": "test",
        "status": RunStatus.COMPLETED,
        "output_text": "Hello",
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _make_config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "test",
        "name": "Test",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "12345",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


class TestUnexpandedEnvVarGuard:
    @pytest.mark.asyncio
    async def test_unexpanded_var_rejected(self, _register_mock_sender):
        """delivery_to containing ${...} is rejected before sending."""
        config = _make_config(delivery_to="${ROBOTHOR_TELEGRAM_CHAT_ID}")
        run = _make_run()
        result = await _deliver_telegram(config, "test message", run)
        assert result is False
        _register_mock_sender.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_chat_id_rejected(self, _register_mock_sender):
        """Empty delivery_to is rejected."""
        config = _make_config(delivery_to="")
        run = _make_run()
        result = await _deliver_telegram(config, "test message", run)
        assert result is False
        _register_mock_sender.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_chat_id_accepted(self, _register_mock_sender):
        """Valid numeric chat_id proceeds to send."""
        config = _make_config(delivery_to="7636850023")
        run = _make_run()
        result = await _deliver_telegram(config, "test message", run)
        assert result is True
        _register_mock_sender.assert_called_once()


class TestFailedRunDelivery:
    """Tests that failed runs with no output still notify the user."""

    @pytest.mark.asyncio
    async def test_error_message_generates_fallback_output(self):
        """A run with error_message but no output_text generates fallback."""
        from robothor.engine.delivery import deliver

        config = _make_config(delivery_mode=DeliveryMode.ANNOUNCE, delivery_to="12345")
        run = _make_run(output_text=None, error_message="Safety limit reached (200 iterations).")
        await deliver(config, run)
        # output_text should have been set to the fallback
        assert run.output_text is not None
        assert "Task incomplete" in run.output_text
        assert "Safety limit" in run.output_text

    @pytest.mark.asyncio
    async def test_no_error_no_output_still_skips(self):
        """A run with no output and no error is still silently skipped."""
        from robothor.engine.delivery import deliver

        config = _make_config(delivery_mode=DeliveryMode.ANNOUNCE, delivery_to="12345")
        run = _make_run(output_text=None, error_message=None)
        result = await deliver(config, run)
        assert result is True
        assert run.delivery_status == "no_output"


class TestMidThoughtDetection:
    """_looks_like_mid_thought catches fragments observed in production.

    Each pattern here is from a real heartbeat beat on 2026-04-20 that
    shipped a mid-chain-of-thought fragment to the operator. The old
    heuristic (AND-gate of opener + ends-with-punct) missed all of these.
    """

    def test_catches_colon_ended_continuation(self):
        """'All 3 deleted. Now reply to the thread: archive:' — trailing colon."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = "All 3 deleted. Now reply to the thread confirming, and archive the emails:"
        assert _looks_like_mid_thought(text)

    def test_catches_dash_ended_continuation(self):
        """'Good — I can see the thread... using the GWS tools —' trailing dash."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = (
            "Good — I can see the thread. The reply came to `bot@example.com`. "
            "Now let me send the reply directly using the GWS tools —"
        )
        assert _looks_like_mid_thought(text)

    def test_catches_reference_to_earlier_report(self):
        """'The verification flags are expected — they're the same issues...' —
        starts with a back-reference that only makes sense mid-conversation."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = "The verification flags are expected — they're the same issues I already reported in Phase 3."
        assert _looks_like_mid_thought(text)

    def test_catches_plain_opener_without_trailing_punct(self):
        """'Now let me do X.' (ends with period) — still a mid-action narration."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = "Now let me send the reply directly."
        assert _looks_like_mid_thought(text)

    def test_catches_trailing_ellipsis(self):
        """Trailing ellipsis alone is a clear mid-thought signal."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = "The next thing I need to do is check the task list..."
        assert _looks_like_mid_thought(text)

    def test_does_not_flag_clean_beat_report(self):
        """A real structured beat report must NOT be flagged."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = (
            "**⚡ MON APR 20 — 6:00 AM ET**\n\n"
            "- 0 open tasks\n"
            "- Fleet green\n"
            "- No anomalies to report."
        )
        assert not _looks_like_mid_thought(text)

    def test_does_not_flag_trivial_quiet_output(self):
        """'All quiet, nothing to report.' — trivial but not a mid-thought."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = "All quiet — nothing actionable this beat."
        assert not _looks_like_mid_thought(text)

    def test_empty_string_is_not_midthought(self):
        from robothor.engine.delivery import _looks_like_mid_thought

        assert not _looks_like_mid_thought("")
        assert not _looks_like_mid_thought("   ")
