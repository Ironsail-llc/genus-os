"""Alert routing and delivery verification (robothor/engine/alerts.py).

Policy: page only what needs the operator. Only ``level='critical'``
goes straight to Telegram; ``'warning'``/``'info'`` become an
``alert_digest`` row in ``crm_agent_notifications`` (to_agent='main'),
which the morning briefing and heartbeat already read.

Delivery is verified, not assumed: ``TelegramBot.send_message`` signals
total failure with an empty result list, and ``alert()`` must treat that
as a failed delivery — writing an ``alert_fallback`` notification row so
the alert still surfaces in the next briefing — and return False.
"""

from __future__ import annotations

import pytest

import robothor.crm.dal as dal
from robothor.engine.alerts import alert
from robothor.engine.delivery import set_telegram_sender


@pytest.fixture()
def notification_rows(monkeypatch):
    """Capture crm_agent_notifications writes instead of touching the DB."""
    rows: list[dict] = []

    def fake_send_notification(**kwargs):
        rows.append(kwargs)
        return "notif-id-1"

    monkeypatch.setattr(dal, "send_notification", fake_send_notification)
    return rows


@pytest.fixture()
def telegram_sends(monkeypatch):
    """Register a fake Telegram sender with the exact production arity."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")
    sends: list[tuple[str, str]] = []

    async def fake_sender(chat_id, text):
        sends.append((chat_id, text))
        return ["message-object"]  # non-empty = delivered

    set_telegram_sender(fake_sender)
    yield sends
    set_telegram_sender(None)  # type: ignore[arg-type]


class TestSeverityRouting:
    @pytest.mark.asyncio
    async def test_warning_goes_to_digest_not_telegram(self, notification_rows, telegram_sends):
        result = await alert("warning", "Tool degradation: x", "details")

        assert result is True
        assert telegram_sends == [], "warning-level alerts must NOT page Telegram"
        assert len(notification_rows) == 1
        row = notification_rows[0]
        assert row["to_agent"] == "main"
        assert row["notification_type"] == "alert_digest"
        assert "Tool degradation: x" in row["subject"]
        assert row["body"] == "details"

    @pytest.mark.asyncio
    async def test_info_goes_to_digest_not_telegram(self, notification_rows, telegram_sends):
        result = await alert("info", "t", "b")

        assert result is True
        assert telegram_sends == []
        assert len(notification_rows) == 1
        assert notification_rows[0]["notification_type"] == "alert_digest"

    @pytest.mark.asyncio
    async def test_critical_pages_telegram_immediately(self, notification_rows, telegram_sends):
        result = await alert("critical", "PostgreSQL down", "3 consecutive ping failures")

        assert result is True
        assert len(telegram_sends) == 1
        chat_id, text = telegram_sends[0]
        assert chat_id == "12345"
        assert "PostgreSQL down" in text
        assert notification_rows == [], "successful page must not also write a fallback row"

    @pytest.mark.asyncio
    async def test_digest_write_failure_returns_false(self, monkeypatch, telegram_sends):
        monkeypatch.setattr(dal, "send_notification", lambda **kw: None)

        result = await alert("warning", "t", "b")

        assert result is False, (
            "send_notification returned None (INSERT refused) — reporting True "
            "would be a control that runs, reports success, and does nothing"
        )


class TestDeliveryVerification:
    @pytest.mark.asyncio
    async def test_empty_send_result_writes_fallback_and_returns_false(
        self, monkeypatch, notification_rows
    ):
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")

        async def failing_sender(chat_id, text):
            return []  # send_message's total-failure signal

        set_telegram_sender(failing_sender)
        try:
            result = await alert("critical", "Engine down", "details")
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

        assert result is False, "an empty result list means nothing was sent"
        assert len(notification_rows) == 1
        row = notification_rows[0]
        assert row["notification_type"] == "alert_fallback"
        assert row["to_agent"] == "main"
        assert "Engine down" in row["subject"]

    @pytest.mark.asyncio
    async def test_sender_exception_writes_fallback_and_returns_false(
        self, monkeypatch, notification_rows
    ):
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")

        async def raising_sender(chat_id, text):
            raise RuntimeError("network down")

        set_telegram_sender(raising_sender)
        try:
            result = await alert("critical", "t", "b")
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

        assert result is False
        assert len(notification_rows) == 1
        assert notification_rows[0]["notification_type"] == "alert_fallback"

    @pytest.mark.asyncio
    async def test_no_sender_registered_writes_fallback(self, monkeypatch, notification_rows):
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")
        set_telegram_sender(None)  # type: ignore[arg-type]

        result = await alert("critical", "t", "b")

        assert result is False
        assert len(notification_rows) == 1
        assert notification_rows[0]["notification_type"] == "alert_fallback"

    @pytest.mark.asyncio
    async def test_fallback_write_failure_still_returns_false(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setattr(dal, "send_notification", lambda **kw: None)

        async def failing_sender(chat_id, text):
            return []

        set_telegram_sender(failing_sender)
        try:
            result = await alert("critical", "t", "b")
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

        assert result is False
