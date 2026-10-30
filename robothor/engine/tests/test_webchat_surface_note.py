"""The model is told how a dashboard chat reply is read.

A webchat reply renders as Markdown in a narrow chat panel. Without guidance
the model answers a simple question with a long multi-section overview; the
note asks for the answer first, short paragraphs, and structure only where it
helps the reader.
"""

from __future__ import annotations

from types import SimpleNamespace

from robothor.engine.models import DeliveryMode, TriggerType
from robothor.engine.prompts import (
    TELEGRAM_SURFACE_NOTE,
    WEBCHAT_SURFACE_NOTE,
    with_surface_note,
)


def _cfg(mode: DeliveryMode = DeliveryMode.NONE, channel: str = "") -> SimpleNamespace:
    return SimpleNamespace(delivery_mode=mode, delivery_channel=channel)


def test_a_webchat_turn_gets_the_webchat_note() -> None:
    turn = with_surface_note("CURRENT USER: the operator", TriggerType.WEBCHAT, _cfg())
    assert turn.startswith("CURRENT USER: the operator")
    assert WEBCHAT_SURFACE_NOTE in turn
    assert TELEGRAM_SURFACE_NOTE not in turn


def test_the_webchat_note_stands_alone_when_there_is_no_preamble() -> None:
    assert with_surface_note("", TriggerType.WEBCHAT, _cfg()) == WEBCHAT_SURFACE_NOTE


def test_webchat_wins_even_when_the_manifest_announces_to_telegram() -> None:
    cfg = _cfg(DeliveryMode.ANNOUNCE, "telegram")
    turn = with_surface_note("", TriggerType.WEBCHAT, cfg)
    assert turn == WEBCHAT_SURFACE_NOTE


def test_a_telegram_turn_keeps_the_telegram_note_only() -> None:
    turn = with_surface_note("x", TriggerType.TELEGRAM, _cfg())
    assert TELEGRAM_SURFACE_NOTE in turn
    assert WEBCHAT_SURFACE_NOTE not in turn


def test_other_triggers_get_no_note() -> None:
    for trigger in (TriggerType.CRON, TriggerType.HOOK, TriggerType.MANUAL, TriggerType.SLACK):
        assert with_surface_note("x", trigger, _cfg()) == "x"


def test_the_webchat_note_is_short_and_names_the_rules_that_matter() -> None:
    note = WEBCHAT_SURFACE_NOTE.lower()
    for rule in ("answer", "paragraph", "heading", "list", "bold"):
        assert rule in note
    assert len(WEBCHAT_SURFACE_NOTE) < 600
