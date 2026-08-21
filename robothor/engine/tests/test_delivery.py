"""Tests for delivery module — unexpanded env var guard, delivery-status truth."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from robothor.engine.chunking import split_telegram_message
from robothor.engine.delivery import _deliver_telegram, set_telegram_sender
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus


class _FakeMessage:
    """Stand-in for an aiogram Message returned by a successful send."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


@pytest.fixture(autouse=True)
def _register_mock_sender():
    """Register a mock Telegram sender for all tests.

    The default return value is a one-message list, matching what
    ``TelegramBot.send_message`` really returns for a single-chunk body — a
    bare ``AsyncMock`` returns a truthy ``MagicMock`` that would let a broken
    delivery-status check look green forever.
    """
    sender = AsyncMock(return_value=[_FakeMessage(1)])
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


class TestDeliveryStatusTruth:
    """``delivery_status`` must be derived from the sender's real result.

    ``TelegramBot.send_message`` swallows every per-chunk exception: it tries
    HTML, retries as plain text, and on double failure logs and returns a list
    that is simply *missing* that chunk (possibly empty). It never raises. So
    a delivery that reached nobody is indistinguishable from a delivered one
    unless the returned list is actually counted — the same assumption that
    hid the alerts arity bug while 432+ pages went nowhere.
    """

    @pytest.mark.asyncio
    async def test_empty_result_is_a_failure(self, _register_mock_sender):
        """Sender returned no messages: nothing reached the operator."""
        _register_mock_sender.return_value = []
        config = _make_config()
        run = _make_run()

        result = await _deliver_telegram(config, "test message", run)

        assert result is False
        assert run.delivery_status is not None
        assert run.delivery_status.startswith("failed:")
        assert "telegram" in run.delivery_status
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_none_result_is_a_failure(self, _register_mock_sender):
        """A sender that returns None never delivered anything either."""
        _register_mock_sender.return_value = None
        config = _make_config()
        run = _make_run()

        result = await _deliver_telegram(config, "test message", run)

        assert result is False
        assert (run.delivery_status or "").startswith("failed:")
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_sender_exception_is_recorded_not_crashed(self, _register_mock_sender):
        """A raising sender is handled, recorded as failed, and never crashes."""
        _register_mock_sender.side_effect = RuntimeError("telegram network down")
        config = _make_config()
        run = _make_run()

        result = await _deliver_telegram(config, "test message", run)

        assert result is False
        assert (run.delivery_status or "").startswith("failed:")
        assert "telegram network down" in (run.delivery_status or "")
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_partial_multi_chunk_send_is_not_delivered(self, _register_mock_sender):
        """3 chunks go out, 2 come back: a truncated briefing is not delivered."""
        body = "x" * (4096 * 2 + 100)
        config = _make_config()
        # Precondition: the body the sender will chunk really is 3 chunks.
        expected_chunks = split_telegram_message(f"*{config.name}*\n\n{body}")
        assert len(expected_chunks) == 3

        _register_mock_sender.return_value = [_FakeMessage(11), _FakeMessage(12)]
        run = _make_run()

        result = await _deliver_telegram(config, body, run)

        assert result is False
        assert run.delivery_status == "partial:2/3"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_full_multi_chunk_send_is_delivered(self, _register_mock_sender):
        """All 3 chunks acknowledged: that is a real delivery."""
        body = "x" * (4096 * 2 + 100)
        _register_mock_sender.return_value = [
            _FakeMessage(11),
            _FakeMessage(12),
            _FakeMessage(13),
        ]
        config = _make_config()
        run = _make_run()

        result = await _deliver_telegram(config, body, run)

        assert result is True
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None

    @pytest.mark.asyncio
    async def test_happy_path_single_chunk(self, _register_mock_sender):
        """One chunk out, one message back: delivered, with a timestamp."""
        config = _make_config()
        run = _make_run()

        result = await _deliver_telegram(config, "short message", run)

        assert result is True
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None
        assert run.delivery_channel == "telegram"

    @pytest.mark.asyncio
    async def test_missing_sender_is_recorded(self):
        """No registered sender is a delivery failure, not a silent no-op."""
        set_telegram_sender(None)  # type: ignore[arg-type]
        config = _make_config()
        run = _make_run()

        result = await _deliver_telegram(config, "test message", run)

        assert result is False
        assert (run.delivery_status or "").startswith("failed:")

    @pytest.mark.asyncio
    async def test_unexpanded_chat_id_is_recorded(self, _register_mock_sender):
        """The ${VAR} guard must leave a status behind, not a NULL column."""
        config = _make_config(delivery_to="${ROBOTHOR_TELEGRAM_CHAT_ID}")
        run = _make_run()

        result = await _deliver_telegram(config, "test message", run)

        assert result is False
        assert (run.delivery_status or "").startswith("failed:")

    @pytest.mark.asyncio
    async def test_failed_send_does_not_surface_to_channel_bus(
        self, _register_mock_sender, monkeypatch
    ):
        """Nothing landed, so nothing may be recorded as said."""
        calls: list[dict] = []

        async def _record(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        monkeypatch.setattr("robothor.engine.delivery._dispatch_post_delivery", _record)
        _register_mock_sender.return_value = []

        await _deliver_telegram(_make_config(), "test message", _make_run())

        assert calls == []

    @pytest.mark.asyncio
    async def test_partial_send_still_maps_the_ids_that_landed(
        self, _register_mock_sender, monkeypatch
    ):
        """The chunks that did land must stay resolvable for reply-to."""
        calls: list[dict] = []

        async def _record(**kwargs: object) -> None:
            calls.append(dict(kwargs))

        monkeypatch.setattr("robothor.engine.delivery._dispatch_post_delivery", _record)
        _register_mock_sender.return_value = [_FakeMessage(11), _FakeMessage(12)]

        await _deliver_telegram(_make_config(), "x" * (4096 * 2 + 100), _make_run())

        assert len(calls) == 1
        assert calls[0]["platform_message_ids"] == ["11", "12"]


class TestEventBusDeliveryStatusTruth:
    """``publish()`` returns ``str | None`` and never raises — so a 'published'
    status written without checking the return value is a guess."""

    @pytest.mark.asyncio
    async def test_publish_returning_none_is_a_failure(self, monkeypatch):
        from robothor.engine import delivery as delivery_mod

        monkeypatch.setattr("robothor.events.bus.publish", lambda **kw: None)
        config = _make_config(delivery_mode=DeliveryMode.LOG)
        run = _make_run()

        result = await delivery_mod._deliver_event_bus(config, "text", run)

        assert result is False
        assert (run.delivery_status or "").startswith("failed:")

    @pytest.mark.asyncio
    async def test_publish_returning_id_is_published(self, monkeypatch):
        from robothor.engine import delivery as delivery_mod

        monkeypatch.setattr("robothor.events.bus.publish", lambda **kw: "1699-0")
        config = _make_config(delivery_mode=DeliveryMode.LOG)
        run = _make_run()

        result = await delivery_mod._deliver_event_bus(config, "text", run)

        assert result is True
        assert run.delivery_status == "published"
        assert run.delivery_channel == "event_bus"

    @pytest.mark.asyncio
    async def test_publish_raising_is_recorded(self, monkeypatch):
        from robothor.engine import delivery as delivery_mod

        def _boom(**kw: object) -> str:
            raise RuntimeError("redis down")

        monkeypatch.setattr("robothor.events.bus.publish", _boom)
        config = _make_config(delivery_mode=DeliveryMode.LOG)
        run = _make_run()

        result = await delivery_mod._deliver_event_bus(config, "text", run)

        assert result is False
        assert "redis down" in (run.delivery_status or "")

    @pytest.mark.asyncio
    async def test_disabled_bus_is_not_reported_as_published(self, monkeypatch):
        """A disabled bus swallows the publish — say so instead of 'published'."""
        from robothor.engine import delivery as delivery_mod

        monkeypatch.setattr("robothor.events.bus.EVENT_BUS_ENABLED", False)
        config = _make_config(delivery_mode=DeliveryMode.LOG)
        run = _make_run()

        result = await delivery_mod._deliver_event_bus(config, "text", run)

        assert result is False
        assert run.delivery_status == "failed:event_bus_disabled"


class TestRealSendPathProbe:
    """End-to-end probe against the REAL ``TelegramBot.send_message``.

    Everything above stubs the sender. These fire an actual Telegram API
    failure through the real swallow path — only the aiogram transport is
    faked — because a test that mocks the failing dependency certifies a
    broken control forever. ``send_message`` catches the exception, retries
    as plain text, catches again, logs, and returns a SHORT list; delivery
    must read that as a failure.
    """

    @pytest.fixture
    def bot(self, engine_config):
        from unittest.mock import MagicMock, patch

        from robothor.engine.telegram import TelegramBot

        with (
            patch("robothor.engine.telegram.Bot") as mock_bot_cls,
            patch("robothor.engine.telegram.Dispatcher"),
        ):
            transport = MagicMock()
            mock_bot_cls.return_value = transport
            bot = TelegramBot(engine_config, MagicMock())
            bot.bot = transport
            yield bot

    @pytest.mark.asyncio
    async def test_total_api_failure_is_never_reported_delivered(self, bot):
        """Every chunk rejected by Telegram: the run must not claim delivery."""
        from robothor.engine.delivery import deliver

        async def _always_fails(**kwargs: object) -> None:
            raise RuntimeError("Bad Request: chat not found")

        bot.bot.send_message = _always_fails
        set_telegram_sender(bot.send_message)

        config = _make_config()
        run = _make_run(output_text="Morning briefing: 3 tasks, 1 PR open.")

        result = await deliver(config, run)

        assert result is False
        assert run.delivery_status == "failed:telegram_send"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_one_bad_chunk_makes_the_briefing_partial(self, bot):
        """Chunk 2 of 3 is rejected: a truncated briefing is not delivered."""
        from robothor.engine.delivery import deliver

        attempts = {"n": 0}

        async def _second_chunk_fails(**kwargs: object) -> _FakeMessage:
            attempts["n"] += 1
            # Attempts 2 and 3 are the HTML and plain-text tries for chunk 2.
            if attempts["n"] in (2, 3):
                raise RuntimeError("Bad Request: message text is too long")
            return _FakeMessage(attempts["n"])

        bot.bot.send_message = _second_chunk_fails
        set_telegram_sender(bot.send_message)

        config = _make_config()
        body = "x" * (4096 * 2 + 100)
        assert len(split_telegram_message(f"*{config.name}*\n\n{body}")) == 3
        run = _make_run(output_text=body)

        result = await deliver(config, run)

        assert result is False
        assert run.delivery_status == "partial:2/3"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_html_failure_that_recovers_as_plain_text_is_delivered(self, bot):
        """The plain-text retry is a real success — don't cry wolf on it."""
        from robothor.engine.delivery import deliver

        attempts = {"n": 0}

        async def _html_fails_once(**kwargs: object) -> _FakeMessage:
            attempts["n"] += 1
            if kwargs.get("parse_mode") is not None:
                raise RuntimeError("Bad Request: can't parse entities")
            return _FakeMessage(attempts["n"])

        bot.bot.send_message = _html_fails_once
        set_telegram_sender(bot.send_message)

        run = _make_run(output_text="Report with <unclosed markup")

        result = await deliver(_make_config(), run)

        assert result is True
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None
