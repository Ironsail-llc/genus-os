"""Telegram voice-note intake stub (Wave-1 hardening, PR-18).

Voice/video notes had no handler, so they were silently dropped. A sibling
handler now acknowledges them; transcription is gated on
ROBOTHOR_VOICE_NOTES_ENABLED (no STT provider is wired yet).
"""

from __future__ import annotations

import inspect

from robothor.engine import telegram, telegram_handlers


def test_voice_handler_registered():
    # Registration TABLE (filter expression) lives in telegram.py; the handler
    # BODY lives in telegram_handlers (phase 3b decomposition).
    reg_src = inspect.getsource(telegram)
    assert "F.voice | F.video_note" in reg_src
    src = inspect.getsource(telegram_handlers)
    assert "handle_voice" in src


def test_gated_on_env_flag():
    src = inspect.getsource(telegram_handlers)
    assert "ROBOTHOR_VOICE_NOTES_ENABLED" in src


def test_no_longer_silently_dropped():
    """The handler answers in both flag states (acknowledgement, not silence)."""
    src = inspect.getsource(telegram_handlers)
    # both the disabled and enabled branches call message.answer(...)
    handler = src[src.index("async def handle_voice") : src.index("async def handle_voice") + 1200]
    assert handler.count("message.answer(") >= 2
