"""Unit tests for robothor/engine/alerts.py.

Covers the public ``alert()`` function and both delivery back-ends:
- _send_telegram  (via the registered sender callable)
- _send_webhook   (via httpx.AsyncClient)
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.alerts import alert
from robothor.engine.delivery import set_telegram_sender

# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_httpx_response(status_code: int) -> MagicMock:
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# ─── Telegram channel tests ──────────────────────────────────────────────────


class TestAlertTelegramChannel:
    """Tests for alert(..., channel="telegram")."""

    @pytest.fixture(autouse=True)
    def _setup_sender(self):
        """Register a mock Telegram sender before each test; tear down after."""
        sender = AsyncMock()
        set_telegram_sender(sender)
        yield sender
        set_telegram_sender(None)  # type: ignore[arg-type]

    # -- happy paths --

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, _setup_sender):
        result = await alert("info", "Test title", "Test body")
        assert result is True

    @pytest.mark.asyncio
    async def test_default_channel_is_telegram(self, _setup_sender):
        """Omitting channel= should hit Telegram."""
        result = await alert("warning", "Default channel", "body")
        assert result is True
        _setup_sender.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_info_icon_in_message(self, _setup_sender):
        await alert("info", "Hello", "world")
        call_args = _setup_sender.call_args
        message = call_args[0][0]
        assert "ℹ️" in message

    @pytest.mark.asyncio
    async def test_warning_icon_in_message(self, _setup_sender):
        await alert("warning", "Watch out", "details")
        message = _setup_sender.call_args[0][0]
        assert "⚠️" in message

    @pytest.mark.asyncio
    async def test_critical_icon_in_message(self, _setup_sender):
        await alert("critical", "Down!", "service unreachable")
        message = _setup_sender.call_args[0][0]
        assert "🚨" in message

    @pytest.mark.asyncio
    async def test_unknown_level_uses_question_mark_icon(self, _setup_sender):
        await alert("debug", "Odd level", "body")
        message = _setup_sender.call_args[0][0]
        assert "❓" in message

    @pytest.mark.asyncio
    async def test_title_in_bold_html(self, _setup_sender):
        await alert("info", "My Title", "some body")
        message = _setup_sender.call_args[0][0]
        assert "<b>My Title</b>" in message

    @pytest.mark.asyncio
    async def test_title_is_html_escaped(self, _setup_sender):
        await alert("info", "<script>xss</script>", "body")
        message = _setup_sender.call_args[0][0]
        assert "<script>" not in message
        assert "&lt;script&gt;" in message

    @pytest.mark.asyncio
    async def test_body_is_html_escaped(self, _setup_sender):
        await alert("warning", "Title", "a < b & c > d")
        message = _setup_sender.call_args[0][0]
        assert "a &lt; b &amp; c &gt; d" in message

    @pytest.mark.asyncio
    async def test_body_appears_in_message(self, _setup_sender):
        await alert("info", "T", "The quick brown fox")
        message = _setup_sender.call_args[0][0]
        assert "The quick brown fox" in message

    # -- no sender registered --

    @pytest.mark.asyncio
    async def test_returns_false_when_no_sender(self):
        """With no Telegram sender, alert must return False (not raise)."""
        set_telegram_sender(None)  # type: ignore[arg-type]
        result = await alert("critical", "No sender", "oops")
        assert result is False

    @pytest.mark.asyncio
    async def test_logs_warning_when_no_sender(self, caplog):
        set_telegram_sender(None)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            await alert("critical", "No sender", "oops")
        assert any("not initialized" in r.message for r in caplog.records)

    # -- sender raises an exception --

    @pytest.mark.asyncio
    async def test_returns_false_on_sender_exception(self, _setup_sender):
        """If the Telegram send raises, alert must return False (not propagate)."""
        _setup_sender.side_effect = RuntimeError("network error")
        result = await alert("info", "Title", "body")
        assert result is False

    @pytest.mark.asyncio
    async def test_logs_warning_on_sender_exception(self, _setup_sender, caplog):
        _setup_sender.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            await alert("info", "Title", "body")
        assert any("Telegram" in r.message for r in caplog.records)


# ─── Webhook channel tests ───────────────────────────────────────────────────


class TestAlertWebhookChannel:
    """Tests for alert(..., channel="webhook")."""

    @pytest.fixture(autouse=True)
    def _clear_sender(self):
        """Ensure Telegram sender is empty so we don't accidentally use it."""
        set_telegram_sender(None)  # type: ignore[arg-type]
        yield
        set_telegram_sender(None)  # type: ignore[arg-type]

    # -- no URL configured --

    @pytest.mark.asyncio
    async def test_returns_false_when_no_url(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove the key if present
            import os

            os.environ.pop("ROBOTHOR_ALERT_WEBHOOK_URL", None)
            result = await alert("info", "Title", "body", channel="webhook")
        assert result is False

    @pytest.mark.asyncio
    async def test_logs_debug_when_no_url(self, caplog):
        import os

        os.environ.pop("ROBOTHOR_ALERT_WEBHOOK_URL", None)
        with caplog.at_level(logging.DEBUG, logger="robothor.engine.alerts"):
            await alert("info", "Title", "body", channel="webhook")
        assert any("ROBOTHOR_ALERT_WEBHOOK_URL" in r.message for r in caplog.records)

    # -- URL configured, happy path --

    @pytest.mark.asyncio
    async def test_returns_true_on_2xx_response(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await alert("critical", "Down", "details", channel="webhook")

        assert result is True

    @pytest.mark.asyncio
    async def test_payload_contains_all_fields(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                await alert("warning", "MyTitle", "MyBody", channel="webhook")

        call_kwargs = mock_client.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["level"] == "warning"
        assert payload["title"] == "MyTitle"
        assert payload["body"] == "MyBody"
        assert payload["metadata"] == {}

    @pytest.mark.asyncio
    async def test_metadata_forwarded_in_payload(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        meta = {"source": "watchdog", "host": "db-01"}
        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                await alert("critical", "DB down", "ping failed", channel="webhook", metadata=meta)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["metadata"] == meta

    @pytest.mark.asyncio
    async def test_posts_to_correct_url(self):
        url = "https://hooks.example.com/alerts"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(204))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": url}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                await alert("info", "T", "B", channel="webhook")

        mock_client.post.assert_awaited_once()
        called_url = mock_client.post.call_args[0][0]
        assert called_url == url

    # -- 4xx / 5xx responses --

    @pytest.mark.asyncio
    async def test_returns_false_on_4xx_response(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(400))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await alert("info", "T", "B", channel="webhook")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_5xx_response(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(503))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await alert("critical", "T", "B", channel="webhook")

        assert result is False

    # -- network exceptions --

    @pytest.mark.asyncio
    async def test_returns_false_on_network_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=OSError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await alert("critical", "T", "B", channel="webhook")

        assert result is False

    @pytest.mark.asyncio
    async def test_logs_warning_on_network_error(self, caplog):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=OSError("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
                    await alert("critical", "T", "B", channel="webhook")

        assert any("webhook" in r.message.lower() for r in caplog.records)

    # -- 399 boundary (should be True) --

    @pytest.mark.asyncio
    async def test_returns_true_on_399_response(self):
        """Status 399 is < 400, so it counts as success."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_httpx_response(399))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"ROBOTHOR_ALERT_WEBHOOK_URL": "https://example.com/hook"}):
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await alert("info", "T", "B", channel="webhook")

        assert result is True


# ─── Unknown channel tests ───────────────────────────────────────────────────


class TestAlertUnknownChannel:
    """Tests for alert(..., channel="<unknown>")."""

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_channel(self):
        result = await alert("info", "Title", "body", channel="pagerduty")
        assert result is False

    @pytest.mark.asyncio
    async def test_logs_warning_for_unknown_channel(self, caplog):
        with caplog.at_level(logging.WARNING, logger="robothor.engine.alerts"):
            await alert("info", "Title", "body", channel="slack")
        assert any("slack" in r.message for r in caplog.records)
