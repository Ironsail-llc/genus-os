"""Telegram voice-note intake (Wave-1 hardening PR-18; rewritten 2026-09-16).

Voice/video notes had no handler at all, so they were silently dropped. A
sibling handler acknowledges them; transcription is still gated on
ROBOTHOR_VOICE_NOTES_ENABLED because no STT provider is wired yet.

The handler moved to ``telegram_attachments`` with the rest of the intake — it
calls ``media_ref``, ``_keep_attachment`` and the same 20 MB ceiling, so it is
the same cluster. These tests moved with it, and stopped being source greps on
the way: two of the three asserted that a STRING appeared in a module, which
says nothing about whether the operator gets an answer.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import telegram, telegram_attachments


def _message(voice=None, video_note=None):
    msg = MagicMock()
    msg.from_user.id = 1001
    msg.chat.id = 100200300
    msg.voice = voice
    msg.video_note = video_note
    # Every media field explicitly, including `video`: a MagicMock invents any
    # attribute it is asked for, so an unset one is TRUTHY and `media_ref`
    # would pick that phantom over the voice note.
    msg.document = None
    msg.photo = None
    msg.audio = None
    msg.video = None
    msg.sticker = None
    msg.answer = AsyncMock()
    return msg


def _voice(size=2048):
    media = MagicMock()
    media.file_id = "AwAC1"
    media.file_unique_id = "AgACvoice"
    media.file_size = size
    media.mime_type = "audio/ogg"
    media.file_name = None  # aiogram's Voice has none
    return media


@pytest.fixture
def bot(engine_config):
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        fake = MagicMock()
        mock_bot_cls.return_value = fake
        instance = telegram.TelegramBot(engine_config, MagicMock())
        instance.bot = fake
        instance.bot.get_file = AsyncMock(return_value=MagicMock(file_path="voice/f.ogg"))

        async def _download(path, destination):
            destination.write(b"OggS fake audio")
            return destination

        instance.bot.download_file = AsyncMock(side_effect=_download)
        yield instance


def test_voice_handler_registered():
    """The registration TABLE is still in telegram.py; the BODY moved."""
    assert "F.voice | F.video_note" in inspect.getsource(telegram)
    assert hasattr(telegram.TelegramBot, "handle_voice")
    assert "handle_voice" in inspect.getsource(telegram_attachments)


class TestDisabled:
    @pytest.mark.asyncio
    async def test_it_answers_rather_than_dropping_the_message(self, bot) -> None:
        msg = _message(voice=_voice())
        with patch.dict("os.environ", {"ROBOTHOR_VOICE_NOTES_ENABLED": ""}):
            await bot.handle_voice(msg)
        msg.answer.assert_awaited_once()
        assert "can't process voice notes yet" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_nothing_is_downloaded(self, bot) -> None:
        with patch.dict("os.environ", {"ROBOTHOR_VOICE_NOTES_ENABLED": ""}):
            await bot.handle_voice(_message(voice=_voice()))
        bot.bot.get_file.assert_not_awaited()


class TestEnabled:
    @pytest.mark.asyncio
    async def test_the_recording_is_kept(self, bot) -> None:
        """The point of the move: an audio file the operator sent once and
        cannot resend is the loss this whole change is about."""
        from pathlib import Path

        msg = _message(voice=_voice())
        with patch.dict("os.environ", {"ROBOTHOR_VOICE_NOTES_ENABLED": "1"}):
            await bot.handle_voice(msg)
        saved = list((Path(bot.config.workspace) / "inbox" / "telegram").rglob("*.ogg"))
        assert len(saved) == 1
        assert saved[0].read_bytes() == b"OggS fake audio"
        assert "saved" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unreadable_note_says_so(self, bot) -> None:
        msg = _message()  # neither voice nor video_note
        with patch.dict("os.environ", {"ROBOTHOR_VOICE_NOTES_ENABLED": "1"}):
            await bot.handle_voice(msg)
        assert "couldn't read" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_declared_oversize_note_names_the_limit(self, bot) -> None:
        from robothor.engine import attachments

        msg = _message(voice=_voice(size=attachments.MAX_DOWNLOAD_BYTES + 1))
        with patch.dict("os.environ", {"ROBOTHOR_VOICE_NOTES_ENABLED": "1"}):
            await bot.handle_voice(msg)
        assert "20 MB" in msg.answer.await_args.args[0]
        bot.bot.get_file.assert_not_awaited()
