"""Tests for robothor/engine/alerts.py — centralized alert dispatcher."""

from __future__ import annotations

import html
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.alerts import _send_telegram, _send_webhook, alert

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_telegram_sender():
    """Register a fresh AsyncMock as the Telegram sender and clean up after."""
    from robothor.engine.delivery import set_telegram_sender

    sender = AsyncMock()
    set_telegram_sender(sender)
    yield sender
    set_telegram_sender(None)  # type: ignore[arg-type]


@pytest.fixture()
def no_telegram_sender():
    """Ensure no Telegram sender is registered."""
    from robothor.engine.delivery import set_telegram_sender

    set_telegram_sender(None)  # type: ignore[arg-type]
    yield
    set_telegram_sender(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests for alert() — top-level dispatcher
# ---------------------------------------------------------------------------


class TestAlertDispatcher:
    """Tests for the public alert() function routing logic."""

    @pytest.mark.asyncio
    async def test_telegram_channel_is_default(self, mock_telegram_sender):
        """alert() defaults to the telegram channel when channel is omitted."""
        result = await alert("info", "Test", "body text")
        assert result is True
        mock_telegram_sender.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_telegram_channel(self, mock_telegram_sender):
        """alert(..., channel='telegram') reaches the Telegram sender."""
        result = await alert("warning", "Watch out", "details", channel="telegram")
        assert result is True
        mock_telegram_sender.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_channel_returns_false(self, caplog):
        """Unknown channel logs a warning and returns False."""
        with caplog.at_level(logging.WARNING):
            result = await alert("info", "Hello", "body", channel="slack_unsupported")
        assert result is False
        assert "Unknown alert channel" in caplog.text

    @pytest.mark.asyncio
    async def test_webhook_channel_dispatches_to_webhook(self):
        """alert(..., channel='webhook') delegates to _send_webhook."""
        with patch(
            "robothor.engine.alerts._send_webhook", new=AsyncMock(return_value=True)
        ) as mock_wh:
            result = await alert(
                "critical",
                "Down",
                "details",
                channel="webhook",
                metadata={"host": "db1"},
            )
        assert result is True
        mock_wh.assert_called_once_with("critical", "Down", "details", {"host": "db1"})

    @pytest.mark.asyncio
    async def test_metadata_passed_to_webhook(self):
        """metadata kwarg is forwarded to _send_webhook."""
        meta = {"env": "prod", "region": "us-east-1"}
        with patch(
            "robothor.engine.alerts._send_webhook", new=AsyncMock(return_value=False)
        ) as mock_wh:
            await alert("warning", "Title", "Body", channel="webhook", metadata=meta)
        _, _, _, passed_meta = mock_wh.call_args.args
        assert passed_meta == meta

    @pytest.mark.asyncio
    async def test_metadata_defaults_to_none_for_telegram(self, mock_telegram_sender):
        """metadata is silently ignored when channel is telegram."""
        result = await alert("info", "Hello", "World", channel="telegram", metadata={"k": "v"})
        assert result is True


# ---------------------------------------------------------------------------
# Tests for _send_telegram()
# ---------------------------------------------------------------------------


class TestSendTelegram:
    """Tests for the Telegram delivery path."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, mock_telegram_sender):
        """Returns True when the sender coroutine completes without error."""
        result = await _send_telegram("info", "Title", "Body")
        assert result is True

    @pytest.mark.asyncio
    async def test_sender_called_with_formatted_message(self, mock_telegram_sender):
        """The sender is called exactly once with the formatted HTML string."""
        await _send_telegram("info", "My Title", "My Body")
        mock_telegram_sender.assert_called_once()
        call_arg = mock_telegram_sender.call_args.args[0]
        assert "<b>" in call_arg
        assert "My Title" in call_arg
        assert "My Body" in call_arg

    @pytest.mark.asyncio
    async def test_info_icon(self, mock_telegram_sender):
        """info level gets the ℹ️ icon."""
        await _send_telegram("info", "t", "b")
        message = mock_telegram_sender.call_args.args[0]
        assert "ℹ️" in message

    @pytest.mark.asyncio
    async def test_warning_icon(self, mock_telegram_sender):
        """warning level gets the ⚠️ icon."""
        await _send_telegram("warning", "t", "b")
        message = mock_telegram_sender.call_args.args[0]
        assert "⚠️" in message

    @pytest.mark.asyncio
    async def test_critical_icon(self, mock_telegram_sender):
        """critical level gets the 🚨 icon."""
        await _send_telegram("critical", "t", "b")
        message = mock_telegram_sender.call_args.args[0]
        assert "🚨" in message

    @pytest.mark.asyncio
    async def test_unknown_level_uses_fallback_icon(self, mock_telegram_sender):
        """Unrecognised level falls back to ❓."""
        await _send_telegram("debug", "t", "b")
        message = mock_telegram_sender.call_args.args[0]
        assert "❓" in message

    @pytest.mark.asyncio
    async def test_title_html_escaped(self, mock_telegram_sender):
        """HTML special chars in the title are escaped."""
        await _send_telegram("info", "<script>alert('xss')</script>", "body")
        message = mock_telegram_sender.call_args.args[0]
        assert "<script>" not in message
        assert html.escape("<script>alert('xss')</script>") in message

    @pytest.mark.asyncio
    async def test_body_html_escaped(self, mock_telegram_sender):
        """HTML special chars in the body are escaped."""
        await _send_telegram("info", "title", "<b>bold & dangerous</b>")
        message = mock_telegram_sender.call_args.args[0]
        assert "<b>bold" not in message
        assert html.escape("<b>bold & dangerous</b>") in message

    @pytest.mark.asyncio
    async def test_returns_false_when_sender_is_none(self, no_telegram_sender, caplog):
        """Returns False and logs a warning when no sender is registered."""
        with caplog.at_level(logging.WARNING):
            result = await _send_telegram("info", "t", "b")
        assert result is False
        assert "Telegram sender not initialized" in caplog.text

    @pytest.mark.asyncio
    async def test_returns_false_on_sender_exception(self, mock_telegram_sender, caplog):
        """Returns False and logs a warning when the sender raises."""
        mock_telegram_sender.side_effect = RuntimeError("network error")
        with caplog.at_level(logging.WARNING):
            result = await _send_telegram("critical", "t", "b")
        assert result is False
        assert "Alert delivery to Telegram failed" in caplog.text


# ---------------------------------------------------------------------------
# Tests for _send_webhook()
# ---------------------------------------------------------------------------


class TestSendWebhook:
    """Tests for the webhook delivery path."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_url_configured(self, caplog, monkeypatch):
        """Returns False without raising when ROBOTHOR_ALERT_WEBHOOK_URL is absent."""
        monkeypatch.delenv("ROBOTHOR_ALERT_WEBHOOK_URL", raising=False)
        with caplog.at_level(logging.DEBUG):
            result = await _send_webhook("info", "t", "b", None)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_2xx_response(self, monkeypatch):
        """Returns True when the HTTP response status is < 400."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://hooks.example.com/test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _send_webhook("critical", "Down", "Details", None)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_4xx_response(self, monkeypatch):
        """Returns False when the HTTP response status is >= 400."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://hooks.example.com/test")
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _send_webhook("warning", "Forbidden", "body", None)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_5xx_response(self, monkeypatch):
        """Returns False when the HTTP response status is 5xx."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://hooks.example.com/test")
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _send_webhook("critical", "Outage", "body", None)
        assert result is False

    @pytest.mark.asyncio
    async def test_payload_contains_all_fields(self, monkeypatch):
        """The POST payload includes level, title, body, and metadata."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://hooks.example.com/test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        meta = {"host": "db01", "region": "eu-west-1"}
        with patch("httpx.AsyncClient", return_value=mock_client):
            await _send_webhook("warning", "MyTitle", "MyBody", meta)
        _, call_kwargs = mock_client.post.call_args
        payload = call_kwargs["json"]
        assert payload["level"] == "warning"
        assert payload["title"] == "MyTitle"
        assert payload["body"] == "MyBody"
        assert payload["metadata"] == meta

    @pytest.mark.asyncio
    async def test_none_metadata_becomes_empty_dict(self, monkeypatch):
        """None metadata is coerced to {} in the payload."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://hooks.example.com/test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            await _send_webhook("info", "t", "b", None)
        _, call_kwargs = mock_client.post.call_args
        assert call_kwargs["json"]["metadata"] == {}

    @pytest.mark.asyncio
    async def test_returns_false_on_network_exception(self, monkeypatch, caplog):
        """Returns False and logs a warning when httpx raises."""
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", "https://hooks.example.com/test")
        with patch("httpx.AsyncClient", side_effect=Exception("connection refused")):
            with caplog.at_level(logging.WARNING):
                result = await _send_webhook("critical", "t", "b", None)
        assert result is False
        assert "Alert delivery to webhook failed" in caplog.text

    @pytest.mark.asyncio
    async def test_post_uses_configured_url(self, monkeypatch):
        """The httpx POST is made to ROBOTHOR_ALERT_WEBHOOK_URL."""
        webhook_url = "https://hooks.example.com/my-endpoint"
        monkeypatch.setenv("ROBOTHOR_ALERT_WEBHOOK_URL", webhook_url)
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            await _send_webhook("info", "t", "b", None)
        called_url = mock_client.post.call_args.args[0]
        assert called_url == webhook_url
