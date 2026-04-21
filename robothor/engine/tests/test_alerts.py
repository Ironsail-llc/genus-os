"""Tests for robothor/engine/alerts.py — centralized alert utility."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.alerts import _send_telegram, _send_webhook, alert
from robothor.engine.delivery import set_telegram_sender

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_telegram_sender():
    """Ensure the Telegram sender is reset to None after every test."""
    yield
    set_telegram_sender(None)  # type: ignore[arg-type]


@pytest.fixture
def mock_sender():
    """Register a mock Telegram sender and return it."""
    sender = AsyncMock()
    set_telegram_sender(sender)
    return sender


# ---------------------------------------------------------------------------
# Tests for alert() routing
# ---------------------------------------------------------------------------


class TestAlertRouting:
    """Tests for the top-level alert() dispatcher."""

    @pytest.mark.asyncio
    async def test_routes_telegram_channel(self, mock_sender):
        result = await alert("info", "Test", "body", channel="telegram")
        assert result is True
        mock_sender.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_webhook_channel(self):
        """Webhook channel routes to _send_webhook (returns False when no URL set)."""
        result = await alert("info", "Test", "body", channel="webhook")
        # No ROBOTHOR_ALERT_WEBHOOK_URL set → False
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_channel_returns_false(self, mock_sender, caplog):
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            result = await alert("info", "Test", "body", channel="unknown_channel")
        assert result is False
        assert "Unknown alert channel" in caplog.text
        mock_sender.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_channel_is_telegram(self, mock_sender):
        """Omitting channel= should default to 'telegram'."""
        result = await alert("info", "Hello", "World")
        assert result is True
        mock_sender.assert_called_once()

    @pytest.mark.asyncio
    async def test_metadata_passed_to_webhook(self):
        """metadata kwarg is forwarded to _send_webhook (returns False without URL)."""
        result = await alert(
            "critical",
            "DB down",
            "details",
            channel="webhook",
            metadata={"host": "pg-01"},
        )
        assert result is False  # No URL configured — graceful failure


# ---------------------------------------------------------------------------
# Tests for _send_telegram()
# ---------------------------------------------------------------------------


class TestSendTelegram:
    """Unit tests for the _send_telegram internal function."""

    @pytest.mark.asyncio
    async def test_sends_message_via_sender(self, mock_sender):
        result = await _send_telegram("info", "Title", "Body")
        assert result is True
        mock_sender.assert_called_once()

    @pytest.mark.asyncio
    async def test_info_icon_in_message(self, mock_sender):
        await _send_telegram("info", "Title", "Body")
        message = mock_sender.call_args[0][0]
        assert "ℹ️" in message

    @pytest.mark.asyncio
    async def test_warning_icon_in_message(self, mock_sender):
        await _send_telegram("warning", "Title", "Body")
        message = mock_sender.call_args[0][0]
        assert "⚠️" in message

    @pytest.mark.asyncio
    async def test_critical_icon_in_message(self, mock_sender):
        await _send_telegram("critical", "Title", "Body")
        message = mock_sender.call_args[0][0]
        assert "🚨" in message

    @pytest.mark.asyncio
    async def test_unknown_level_uses_question_mark_icon(self, mock_sender):
        await _send_telegram("verbose", "Title", "Body")
        message = mock_sender.call_args[0][0]
        assert "❓" in message

    @pytest.mark.asyncio
    async def test_title_html_escaped(self, mock_sender):
        await _send_telegram("info", "<b>bold</b>", "body")
        message = mock_sender.call_args[0][0]
        assert "&lt;b&gt;bold&lt;/b&gt;" in message
        # The title must not contain raw angle brackets
        assert "<b>bold</b>" not in message

    @pytest.mark.asyncio
    async def test_body_html_escaped(self, mock_sender):
        await _send_telegram("info", "Title", "a & b < c")
        message = mock_sender.call_args[0][0]
        assert "a &amp; b &lt; c" in message

    @pytest.mark.asyncio
    async def test_title_wrapped_in_bold_tag(self, mock_sender):
        await _send_telegram("info", "MyTitle", "body")
        message = mock_sender.call_args[0][0]
        assert "<b>MyTitle</b>" in message

    @pytest.mark.asyncio
    async def test_no_sender_returns_false(self, caplog):
        # No sender registered (_clear_telegram_sender fixture cleared it)
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            result = await _send_telegram("info", "Title", "Body")
        assert result is False
        assert "Telegram sender not initialized" in caplog.text

    @pytest.mark.asyncio
    async def test_sender_exception_returns_false(self, caplog):
        """If the sender raises, _send_telegram catches and returns False."""
        failing_sender = AsyncMock(side_effect=RuntimeError("connection timeout"))
        set_telegram_sender(failing_sender)
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            result = await _send_telegram("critical", "Title", "Body")
        assert result is False
        assert "Alert delivery to Telegram failed" in caplog.text

    @pytest.mark.asyncio
    async def test_import_error_returns_false(self, caplog):
        """If the delivery import fails, _send_telegram returns False."""
        with patch.dict("sys.modules", {"robothor.engine.delivery": None}):
            with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                result = await _send_telegram("info", "Title", "Body")
        assert result is False


# ---------------------------------------------------------------------------
# Tests for _send_webhook()
# ---------------------------------------------------------------------------


class TestSendWebhook:
    """Unit tests for the _send_webhook internal function."""

    @pytest.mark.asyncio
    async def test_no_url_returns_false(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="robothor.engine.alerts"):
            result = await _send_webhook("info", "Title", "Body", None)
        assert result is False
        assert "No ROBOTHOR_ALERT_WEBHOOK_URL" in caplog.text

    @pytest.mark.asyncio
    async def test_successful_post_returns_true(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                result = await _send_webhook("critical", "DB down", "details", {"host": "pg-01"})

        assert result is True

    @pytest.mark.asyncio
    async def test_4xx_response_returns_false(self):
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                result = await _send_webhook("info", "Title", "Body", None)

        assert result is False

    @pytest.mark.asyncio
    async def test_5xx_response_returns_false(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                result = await _send_webhook("warning", "Title", "Body", None)

        assert result is False

    @pytest.mark.asyncio
    async def test_399_response_returns_true(self):
        """Status 399 is < 400, so it should return True."""
        mock_response = MagicMock()
        mock_response.status_code = 399
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                result = await _send_webhook("info", "Title", "Body", None)

        assert result is True

    @pytest.mark.asyncio
    async def test_payload_structure(self):
        """Verify the JSON payload sent to the webhook."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                await _send_webhook("critical", "Disk full", "/ at 99%", {"disk": "sda1"})

        _, kwargs = mock_client.post.call_args
        payload = kwargs["json"]
        assert payload["level"] == "critical"
        assert payload["title"] == "Disk full"
        assert payload["body"] == "/ at 99%"
        assert payload["metadata"] == {"disk": "sda1"}

    @pytest.mark.asyncio
    async def test_none_metadata_becomes_empty_dict(self):
        """metadata=None in the payload should be sent as {}."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                await _send_webhook("info", "Title", "Body", None)

        _, kwargs = mock_client.post.call_args
        assert kwargs["json"]["metadata"] == {}

    @pytest.mark.asyncio
    async def test_network_exception_returns_false(self, caplog):
        """Network errors are caught and return False."""
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch.dict(
            "os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://hooks.example.com/alert"}
        ):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                    result = await _send_webhook("critical", "Title", "Body", None)

        assert result is False
        assert "Alert delivery to webhook failed" in caplog.text

    @pytest.mark.asyncio
    async def test_uses_correct_url(self):
        """The URL from the environment variable is actually used."""
        expected_url = "https://hooks.example.com/my-endpoint"
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": expected_url}):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                await _send_webhook("info", "Title", "Body", None)

        call_args, _ = mock_client.post.call_args
        assert call_args[0] == expected_url
