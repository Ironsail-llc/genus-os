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

    def test_does_not_flag_long_clean_output_starting_with_leader(self):
        """Substantial output (>300 chars) ending cleanly is real content even
        if it happens to start with a narration leader. Observed in production
        on 2026-05-09: ~70% of completed heartbeats start with 'Now let me
        compose the digest.' followed by a full structured digest."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = (
            "Now let me compose the digest. Key findings: "
            + ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 8)
            + "Final report: all good."
        )
        assert len(text) > 300
        assert not _looks_like_mid_thought(text)

    def test_still_flags_short_leader_only_fragment(self):
        """Short narration that ends mid-thought is still a fragment."""
        from robothor.engine.delivery import _looks_like_mid_thought

        text = "Now let me check the inbox—"
        assert _looks_like_mid_thought(text)


class TestStripCotPrefix:
    """_strip_cot_prefix removes the leading narration paragraph that
    MiMo V2.5 Pro reliably emits before every heartbeat digest, even
    though brain/HEARTBEAT.md forbids it.

    Conservative: only strips when the head paragraph looks like
    narration AND the tail starts with a structured marker (emoji,
    header, bullet). A genuine fragment with no structured tail is
    left alone so the existing reframe path can catch it."""

    def test_removes_leading_narration_before_emoji_header(self):
        from robothor.engine.delivery import _strip_cot_prefix

        text = (
            "Now let me compose the digest.\n\n"
            "⚡ SAT 19:03 ET\n"
            "🎯 IN FLIGHT\n"
            "· Nothing actively running"
        )
        out = _strip_cot_prefix(text)
        assert out.startswith("⚡ SAT 19:03 ET")
        assert "Now let me" not in out

    def test_removes_leading_narration_before_bullets(self):
        from robothor.engine.delivery import _strip_cot_prefix

        text = (
            "Now I have all the data. Let me compose the digest.\n\n"
            "- requires_human: 20\n"
            "- in_progress: 3"
        )
        out = _strip_cot_prefix(text)
        assert out.startswith("- requires_human: 20")

    def test_removes_leading_narration_before_markdown_header(self):
        from robothor.engine.delivery import _strip_cot_prefix

        text = (
            "I have all the data I need. Let me compose the digest now.\n\n## Status\n\nAll green."
        )
        out = _strip_cot_prefix(text)
        assert out.startswith("## Status")

    def test_removes_leading_telemetry_narration(self):
        """Observed pattern: 'Fleet delta is +9.9... Now composing the digest.'"""
        from robothor.engine.delivery import _strip_cot_prefix

        text = (
            "Fleet delta is +9.9 (≥5) so I surface it. Hold rate is null. Now composing the digest.\n\n"
            "⚡ SAT 11:03 ET\n· report"
        )
        out = _strip_cot_prefix(text)
        assert out.startswith("⚡ SAT 11:03 ET")

    def test_preserves_substantive_text_without_narration(self):
        from robothor.engine.delivery import _strip_cot_prefix

        text = "⚡ SAT 19:03 ET\n🎯 IN FLIGHT\n· Nothing actively running"
        assert _strip_cot_prefix(text) == text

    def test_does_not_strip_when_tail_is_unstructured(self):
        """Head looks like narration but tail is also prose — could be a
        real two-paragraph report, not safe to strip the first paragraph."""
        from robothor.engine.delivery import _strip_cot_prefix

        text = "Now let me check.\n\nThe server is down."
        assert _strip_cot_prefix(text) == text

    def test_does_not_strip_long_first_paragraph(self):
        """First paragraph >200 chars is real content, not a narration prefix."""
        from robothor.engine.delivery import _strip_cot_prefix

        head = "Now let me compose. " + ("This is real substantive content that runs on. " * 6)
        assert len(head) > 200
        text = f"{head}\n\n⚡ SAT 19:03 ET\n· report"
        assert _strip_cot_prefix(text) == text

    def test_no_double_newline_returns_unchanged(self):
        from robothor.engine.delivery import _strip_cot_prefix

        text = "Now let me compose. ⚡ SAT 19:03 ET"
        assert _strip_cot_prefix(text) == text

    def test_empty_string_returns_unchanged(self):
        from robothor.engine.delivery import _strip_cot_prefix

        assert _strip_cot_prefix("") == ""


class TestHeartbeatDeliveryReframing:
    """Integration: deliver() should ship the clean digest when the model
    leaks a CoT prefix, but still reframe genuine fragments."""

    @pytest.mark.asyncio
    async def test_deliver_heartbeat_strips_cot_prefix_and_does_not_reframe(
        self, _register_mock_sender
    ):
        from robothor.engine.delivery import deliver

        digest_body = (
            "⚡ SAT 19:03 ET\n\n"
            "🎯 IN FLIGHT\n"
            "· Nothing actively running — all 18 items waiting on you\n\n"
            "🤝 YOUR CALL\n"
            "· Acme MSA expires today — $42K/mo billing pauses, need your signature\n"
            "· Alice's server still down since 5am\n\n"
            "📊 Open: 18 · Done today: 6"
        )
        text = f"Now let me compose the digest.\n\n{digest_body}"
        config = _make_config(delivery_mode=DeliveryMode.ANNOUNCE, delivery_to="12345")
        run = _make_run(
            output_text=text,
            trigger_detail="heartbeat:3 6-22 * * *",
        )
        result = await deliver(config, run)
        assert result is True
        _register_mock_sender.assert_called_once()
        sent_body = _register_mock_sender.call_args[0][1]
        assert "⚠️ Beat ended incomplete" not in sent_body
        assert "Now let me compose" not in sent_body
        assert "⚡ SAT 19:03 ET" in sent_body
        assert "📊 Open: 18" in sent_body

    @pytest.mark.asyncio
    async def test_deliver_heartbeat_still_reframes_genuine_fragment(self, _register_mock_sender):
        """Short, mid-thought-only output without a structured tail must
        still trigger the reframe path."""
        from robothor.engine.delivery import deliver

        config = _make_config(delivery_mode=DeliveryMode.ANNOUNCE, delivery_to="12345")
        run = _make_run(
            output_text="Now let me check the inbox—",
            trigger_detail="heartbeat:3 6-22 * * *",
        )
        result = await deliver(config, run)
        assert result is True
        _register_mock_sender.assert_called_once()
        sent_body = _register_mock_sender.call_args[0][1]
        assert "⚠️ Beat ended" in sent_body
