"""Tests for robothor.vision.alerts — AlertManager and handlers."""

from unittest.mock import AsyncMock, patch

import pytest

from robothor.vision.alerts import (
    AlertHandler,
    AlertManager,
    TelegramAlert,
    WebhookAlert,
)

# ─── TelegramAlert ───────────────────────────────────────────────────


class TestTelegramAlert:
    def test_init_with_explicit_tokens(self):
        alert = TelegramAlert(bot_token="test-token", chat_id="12345")
        assert alert.bot_token == "test-token"
        assert alert.chat_id == "12345"

    def test_init_from_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")
        alert = TelegramAlert()
        assert alert.bot_token == "env-token"
        assert alert.chat_id == "env-chat"

    def test_init_defaults_to_empty(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        alert = TelegramAlert()
        assert alert.bot_token == ""
        assert alert.chat_id == ""

    @pytest.mark.asyncio
    async def test_send_skips_when_no_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        alert = TelegramAlert()
        result = await alert.send("test", "Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_text_message(self):
        alert = TelegramAlert(bot_token="test-token", chat_id="12345")
        mock_resp = AsyncMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await alert.send("motion", "Motion detected")
            assert result is True
            mock_client.post.assert_called_once()
            call_url = mock_client.post.call_args[0][0]
            assert "sendMessage" in call_url

    @pytest.mark.asyncio
    async def test_send_photo(self):
        alert = TelegramAlert(bot_token="test-token", chat_id="12345")
        mock_resp = AsyncMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await alert.send("unknown_person", "Unknown!", image_bytes=b"jpeg-data")
            assert result is True
            call_url = mock_client.post.call_args[0][0]
            assert "sendPhoto" in call_url

    @pytest.mark.asyncio
    async def test_send_handles_network_error(self):
        alert = TelegramAlert(bot_token="test-token", chat_id="12345")
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client_cls.return_value = mock_client

            result = await alert.send("test", "Hello")
            assert result is False

    def test_is_alert_handler(self):
        assert issubclass(TelegramAlert, AlertHandler)


# ─── WebhookAlert ────────────────────────────────────────────────────


class TestWebhookAlert:
    def test_init(self):
        alert = WebhookAlert(url="https://example.com/hook")
        assert alert.url == "https://example.com/hook"
        assert alert.headers == {}

    def test_init_with_headers(self):
        alert = WebhookAlert(url="https://example.com", headers={"X-Key": "secret"})
        assert alert.headers == {"X-Key": "secret"}

    @pytest.mark.asyncio
    async def test_send_posts_json(self):
        alert = WebhookAlert(url="https://example.com/hook")
        mock_resp = AsyncMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await alert.send("motion", "Motion detected", metadata={"score": 0.5})
            assert result is True
            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert payload["event_type"] == "motion"
            assert payload["message"] == "Motion detected"
            assert payload["metadata"]["score"] == 0.5

    @pytest.mark.asyncio
    async def test_send_handles_non_200(self):
        alert = WebhookAlert(url="https://example.com/hook")
        mock_resp = AsyncMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await alert.send("test", "Hello")
            assert result is False

    @pytest.mark.asyncio
    async def test_send_handles_network_error(self):
        alert = WebhookAlert(url="https://example.com/hook")
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("timeout"))
            mock_client_cls.return_value = mock_client

            result = await alert.send("test", "Hello")
            assert result is False

    def test_is_alert_handler(self):
        assert issubclass(WebhookAlert, AlertHandler)


# ─── AlertManager ────────────────────────────────────────────────────


class TestAlertManager:
    def test_init_empty(self):
        mgr = AlertManager()
        assert mgr.handlers == []

    def test_add_handler(self):
        mgr = AlertManager()
        handler = TelegramAlert(bot_token="t", chat_id="c")
        mgr.add_handler(handler)
        assert len(mgr.handlers) == 1

    @pytest.mark.asyncio
    async def test_send_to_multiple_handlers(self):
        mgr = AlertManager()
        h1 = AsyncMock(spec=AlertHandler)
        h1.send = AsyncMock(return_value=True)
        h2 = AsyncMock(spec=AlertHandler)
        h2.send = AsyncMock(return_value=True)
        mgr.handlers = [h1, h2]

        count = await mgr.send("test", "Hello")
        assert count == 2
        h1.send.assert_called_once()
        h2.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_counts_successes(self):
        mgr = AlertManager()
        h1 = AsyncMock(spec=AlertHandler)
        h1.send = AsyncMock(return_value=True)
        h2 = AsyncMock(spec=AlertHandler)
        h2.send = AsyncMock(return_value=False)
        mgr.handlers = [h1, h2]

        count = await mgr.send("test", "Hello")
        assert count == 1

    @pytest.mark.asyncio
    async def test_send_handles_handler_exception(self):
        mgr = AlertManager()
        h1 = AsyncMock(spec=AlertHandler)
        h1.send = AsyncMock(side_effect=Exception("Boom"))
        h2 = AsyncMock(spec=AlertHandler)
        h2.send = AsyncMock(return_value=True)
        mgr.handlers = [h1, h2]

        count = await mgr.send("test", "Hello")
        assert count == 1  # h2 still succeeds

    @pytest.mark.asyncio
    async def test_send_no_handlers(self):
        mgr = AlertManager()
        count = await mgr.send("test", "Hello")
        assert count == 0

    @pytest.mark.asyncio
    async def test_send_passes_all_args(self):
        mgr = AlertManager()
        h = AsyncMock(spec=AlertHandler)
        h.send = AsyncMock(return_value=True)
        mgr.handlers = [h]

        await mgr.send("unknown_person", "Alert!", image_bytes=b"img", metadata={"key": "val"})
        h.send.assert_called_once_with("unknown_person", "Alert!", b"img", {"key": "val"})
