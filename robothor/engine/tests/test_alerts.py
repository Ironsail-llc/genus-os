"""Alert delivery has never worked — the sender arity bug.

``alerts._send_telegram`` calls ``await send_fn(message)`` with a single
positional argument, but the registered sender is
``TelegramBot.send_message(self, chat_id: str, text: str, **_ignored)``.
``message`` silently binds to the ``chat_id`` parameter and ``text`` is
missing, so every call raises ``TypeError`` — caught and logged by the
broad ``except Exception`` in ``_send_telegram``, so the failure is
invisible. 432+ real alerts (runaway-token, degraded-model, watchdog
detectors) have never reached the operator.

These tests use a fake sender with the EXACT production arity
(``async def fake_sender(chat_id, text)``) — an ``AsyncMock`` would accept
any call signature and hide the bug entirely.

Only ``level='critical'`` pages Telegram (warning/info go to the
notification digest — see test_alert_routing.py), so these arity tests
exercise the critical path.
"""

from __future__ import annotations

import logging

import pytest

import robothor.crm.dal as dal
from robothor.engine.alerts import alert
from robothor.engine.delivery import set_telegram_sender


@pytest.fixture(autouse=True)
def _capture_fallback_rows(monkeypatch):
    """Failed pages write an alert_fallback notification — keep it off the DB."""
    rows: list[dict] = []
    monkeypatch.setattr(dal, "send_notification", lambda **kw: rows.append(kw) or "notif-1")
    return rows


@pytest.mark.asyncio
async def test_alert_delivers_with_chat_id_and_text(monkeypatch):
    """A real send must happen, with the configured chat id and the title in the text."""
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")

    sends: list[tuple[str, str]] = []

    async def fake_sender(chat_id, text):  # exact production arity — AsyncMock would hide the bug
        sends.append((chat_id, text))
        return ["message-object"]  # non-empty list = delivered

    set_telegram_sender(fake_sender)
    try:
        result = await alert("critical", "t", "b")
    finally:
        set_telegram_sender(None)  # type: ignore[arg-type]

    assert result is True
    assert len(sends) == 1, (
        "the alert sender arity bug (message bound to chat_id, text missing) "
        f"drops every alert silently — sends={sends}"
    )
    sent_chat_id, sent_text = sends[0]
    assert sent_chat_id == "12345"
    assert "t" in sent_text


@pytest.mark.asyncio
async def test_alert_returns_false_when_no_sender_registered(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "12345")

    set_telegram_sender(None)  # type: ignore[arg-type]
    result = await alert("critical", "t", "b")

    assert result is False


@pytest.mark.asyncio
async def test_alert_returns_false_when_no_chat_id_configured(monkeypatch, caplog):
    monkeypatch.delenv("ROBOTHOR_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    sends: list[tuple[str, str]] = []

    async def fake_sender(chat_id, text):
        sends.append((chat_id, text))
        return ["message-object"]

    set_telegram_sender(fake_sender)
    try:
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            result = await alert("critical", "t", "b")
    finally:
        set_telegram_sender(None)  # type: ignore[arg-type]

    assert result is False
    assert sends == []
    assert any("chat_id" in rec.message.lower() for rec in caplog.records), (
        "no chat id configured should degrade gracefully with a warning, not raise"
    )
