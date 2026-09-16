"""``Channel.send_attachment`` — the protocol slot, and who actually fills it.

The outbound half needs a place on the channel contract rather than a Telegram
special case, or the next surface repeats the work and the tool grows a branch
per channel. Telegram implements it. Slack, email, webchat and the event bus
RAISE, and that raise is the contract rather than a gap in it: a channel that
returned a plausible receipt for a file it never sent would be the exact defect
``channels/base.py`` was written against.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.channels.base import SendReceipt
from robothor.engine.channels.telegram import TelegramChannel


class TestProtocol:
    def test_the_slot_is_declared(self) -> None:
        from robothor.engine.channels.base import Channel

        assert hasattr(Channel, "send_attachment")

    @pytest.mark.parametrize(
        "module_name,class_name",
        [
            ("robothor.engine.channels.slack", "SlackChannel"),
            ("robothor.engine.channels.email", "EmailChannel"),
            ("robothor.engine.channels.webchat", "WebchatChannel"),
            ("robothor.engine.channels.event_bus", "EventBusChannel"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_channel_that_cannot_send_files_says_so(self, module_name, class_name) -> None:
        import importlib

        channel = importlib.import_module(module_name).__dict__[class_name]
        with pytest.raises(NotImplementedError) as caught:
            await channel.send_attachment(
                MagicMock(), "target", "/tmp/a.txt", "caption", as_="auto"
            )
        # The refusal names the channel, so an agent reading it knows which
        # surface cannot take a file rather than only that something could not.
        expected = {
            "SlackChannel": "slack",
            "EmailChannel": "email",
            "WebchatChannel": "webchat",
            "EventBusChannel": "event_bus",
        }[class_name]
        assert expected in str(caught.value).lower()


class TestTelegram:
    @pytest.fixture
    def bot(self, monkeypatch, tmp_path):
        fake = MagicMock()
        # SimpleNamespace, not MagicMock: a MagicMock answers `__iter__` with an
        # EMPTY iterator, so `acknowledged_messages` counts zero chunks and the
        # test would assert against a receipt the production shape never
        # produces. aiogram's own Message is not iterable either.
        fake.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=99))
        fake.send_document = AsyncMock(return_value=SimpleNamespace(message_id=101))
        monkeypatch.setattr(
            "robothor.engine.channels.telegram._aiogram_bot", lambda: fake, raising=True
        )
        return fake

    @pytest.mark.asyncio
    async def test_a_photo_goes_through_send_photo(self, tmp_path, bot) -> None:
        path = tmp_path / "chart.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        receipt = await TelegramChannel().send_attachment(
            "100200300", str(path), "look", as_="photo"
        )
        assert isinstance(receipt, SendReceipt)
        assert receipt.acknowledged == 1
        assert receipt.platform_ids == ["99"]
        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.await_args.kwargs["caption"] == "look"

    @pytest.mark.asyncio
    async def test_a_document_goes_through_send_document(self, tmp_path, bot) -> None:
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF")
        receipt = await TelegramChannel().send_attachment(
            "100200300", str(path), "", as_="document"
        )
        assert receipt.platform_ids == ["101"]
        bot.send_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_target_is_a_failed_receipt_not_a_guess(self, tmp_path, bot) -> None:
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        receipt = await TelegramChannel().send_attachment("", str(path), "")
        assert receipt.acknowledged == 0
        assert receipt.status == "failed:telegram_no_chat_id"
        bot.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_raising_bot_is_a_failed_receipt_not_an_exception(self, tmp_path, bot) -> None:
        bot.send_document.side_effect = RuntimeError("network down")
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        receipt = await TelegramChannel().send_attachment("100200300", str(path), "")
        assert receipt.acknowledged == 0
        assert receipt.status and receipt.status.startswith("failed:")

    @pytest.mark.asyncio
    async def test_no_bot_at_all_is_reported_rather_than_assumed(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "robothor.engine.channels.telegram._aiogram_bot", lambda: None, raising=True
        )
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        receipt = await TelegramChannel().send_attachment("100200300", str(path), "")
        assert receipt.acknowledged == 0
        assert receipt.status == "failed:telegram_no_bot"
