"""Tests for robothor/engine/alerts.py — centralized alert utility."""

from __future__ import annotations

import html
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.alerts import _send_telegram, _send_webhook, alert

# Helpers


def _make_telegram_sender(*, raises: Exception | None = None) -> AsyncMock:
    """Return a coroutine mock that optionally raises on call."""
    sender = AsyncMock()
    if raises is not None:
        sender.side_effect = raises
    return sender


# ---------------------------------------------------------------------------
# Tests for alert() — top-level dispatcher
# ---------------------------------------------------------------------------


class TestAlert:
    @pytest.mark.asyncio
    async def test_routes_to_telegram_by_default(self):
        """alert() with channel='telegram' calls _send_telegram."""
        with patch("robothor.engine.alerts._send_telegram", new_callable=AsyncMock) as mock_tg:
            mock_tg.return_value = True
            result = await alert("info", "Title", "Body")
        assert result is True
        mock_tg.assert_awaited_once_with("info", "Title", "Body")

    @pytest.mark.asyncio
    async def test_routes_to_telegram_explicit(self):
        """alert() with channel='telegram' explicitly routes correctly."""
        with patch("robothor.engine.alerts._send_telegram", new_callable=AsyncMock) as mock_tg:
            mock_tg.return_value = True
            result = await alert("warning", "Title", "Body", channel="telegram")
        assert result is True
        mock_tg.assert_awaited_once_with("warning", "Title", "Body")

    @pytest.mark.asyncio
    async def test_routes_to_webhook(self):
        """alert() with channel='webhook' calls _send_webhook."""
        meta = {"key": "value"}
        with patch("robothor.engine.alerts._send_webhook", new_callable=AsyncMock) as mock_wh:
            mock_wh.return_value = True
            result = await alert("critical", "Title", "Body", channel="webhook", metadata=meta)
        assert result is True
        mock_wh.assert_awaited_once_with("critical", "Title", "Body", meta)

    @pytest.mark.asyncio
    async def test_webhook_without_metadata(self):
        """alert() passes None metadata to _send_webhook when not specified."""
        with patch("robothor.engine.alerts._send_webhook", new_callable=AsyncMock) as mock_wh:
            mock_wh.return_value = False
            result = await alert("info", "Title", "Body", channel="webhook")
        assert result is False
        mock_wh.assert_awaited_once_with("info", "Title", "Body", None)

    @pytest.mark.asyncio
    async def test_unknown_channel_returns_false(self, caplog):
        """alert() with an unknown channel logs a warning and returns False."""
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            result = await alert("info", "Title", "Body", channel="slack")
        assert result is False
        assert "Unknown alert channel: slack" in caplog.text


# Tests for _send_telegram()


class TestSendTelegram:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_sender(self, caplog):
        """_send_telegram returns False when Telegram sender is not registered."""
        with (
            patch("robothor.engine.delivery.get_telegram_sender", return_value=None),
            patch(
                "robothor.engine.alerts._send_telegram.__module__",
                "robothor.engine.alerts",
            ),
            patch("robothor.engine.delivery.get_telegram_sender", return_value=None),
        ):
            with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                result = await _send_telegram("info", "Title", "Body")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_sender_is_none(self, caplog):
        """_send_telegram returns False and logs when no sender is available."""
        with patch(
            "robothor.engine.alerts.get_telegram_sender",  # module-level attribute after import
            return_value=None,
            create=True,
        ):
            # Patch at the import site inside _send_telegram
            with patch("robothor.engine.delivery.get_telegram_sender", return_value=None):
                from robothor.engine.delivery import set_telegram_sender

                set_telegram_sender(None)  # type: ignore[arg-type]

                with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                    result = await _send_telegram("info", "No sender", "Details")
        assert result is False

    @pytest.mark.asyncio
    async def test_sends_formatted_html_message(self):
        """_send_telegram formats HTML correctly and calls the sender."""
        sender = AsyncMock()
        from robothor.engine.delivery import set_telegram_sender

        set_telegram_sender(sender)
        try:
            result = await _send_telegram("info", "My Title", "My Body")
            assert result is True
            sender.assert_awaited_once()
            call_args = sender.call_args[0][0]
            assert "ℹ️" in call_args
            assert "<b>My Title</b>" in call_args
            assert "My Body" in call_args
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "level,expected_icon",
        [
            ("info", "ℹ️"),
            ("warning", "⚠️"),
            ("critical", "🚨"),
            ("unknown_level", "❓"),
        ],
    )
    async def test_icon_per_level(self, level: str, expected_icon: str):
        """_send_telegram uses the correct icon for each alert level."""
        sender = AsyncMock()
        from robothor.engine.delivery import set_telegram_sender

        set_telegram_sender(sender)
        try:
            await _send_telegram(level, "T", "B")
            call_args = sender.call_args[0][0]
            assert expected_icon in call_args
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_html_escapes_title_and_body(self):
        """_send_telegram escapes HTML characters in title and body."""
        sender = AsyncMock()
        from robothor.engine.delivery import set_telegram_sender

        set_telegram_sender(sender)
        try:
            title = "<script>alert('xss')</script>"
            body = "a & b > c"
            await _send_telegram("warning", title, body)
            call_args = sender.call_args[0][0]
            assert html.escape(title) in call_args
            assert html.escape(body) in call_args
            # Raw unescaped angle brackets must not appear in the title part
            # (the <b> tag itself is added by the formatter, not user input)
            assert "<script>" not in call_args
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_exception_in_sender_returns_false(self, caplog):
        """_send_telegram catches exceptions from the sender and returns False."""
        sender = AsyncMock(side_effect=RuntimeError("network error"))
        from robothor.engine.delivery import set_telegram_sender

        set_telegram_sender(sender)
        try:
            with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                result = await _send_telegram("critical", "Title", "Body")
            assert result is False
            assert "Alert delivery to Telegram failed" in caplog.text
        finally:
            set_telegram_sender(None)  # type: ignore[arg-type]


# Tests for _send_webhook()


class TestSendWebhook:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_url_configured(self, monkeypatch, caplog):
        """_send_webhook returns False when ROBOTHOR_ALERT_WEBHOOK_URL is unset."""
        monkeypatch.delenv("ROBOTHOR_ALERT_WEBHOOK_URL", raising=False)
        with caplog.at_level(logging.DEBUG, logger="robothor.engine.alerts"):
            result = await _send_webhook("info", "Title", "Body", None)
        assert result is False

    @pytest.mark.asyncio
    async def test_posts_correct_payload(self, monkeypatch):
        """_send_webhook posts the correct JSON payload to the webhook URL."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://example.com/webhook")

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _send_webhook("critical", "Outage", "DB is down", {"service": "db"})

        assert result is True
        mock_client.post.assert_awaited_once_with(
            "https://example.com/webhook",
            json={
                "level": "critical",
                "title": "Outage",
                "body": "DB is down",
                "metadata": {"service": "db"},
            },
        )

    @pytest.mark.asyncio
    async def test_none_metadata_becomes_empty_dict(self, monkeypatch):
        """_send_webhook converts None metadata to empty dict in payload."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://example.com/hook")

        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _send_webhook("info", "T", "B", None)

        assert result is True
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["metadata"] == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_code,expected", [(200, True), (204, True), (400, False), (500, False)]
    )
    async def test_success_based_on_status_code(
        self, monkeypatch, status_code: int, expected: bool
    ):
        """_send_webhook returns True for 2xx/3xx and False for 4xx/5xx status codes."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://example.com/hook")

        mock_response = MagicMock()
        mock_response.status_code = status_code

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _send_webhook("info", "T", "B", None)

        assert result is expected

    @pytest.mark.asyncio
    async def test_exception_returns_false(self, monkeypatch, caplog):
        """_send_webhook catches exceptions and returns False."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://example.com/hook")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                result = await _send_webhook("critical", "T", "B", None)

        assert result is False
        assert "Alert delivery to webhook failed" in caplog.text
