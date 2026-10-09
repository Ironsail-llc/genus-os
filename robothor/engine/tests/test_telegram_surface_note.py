"""The model is told when its words land on a phone.

Live, 2026-10-08: the owner's Telegram and the Helm share one session, so the
model wrote every reply for a wide desktop renderer — five-column tables,
``---`` dividers, bold on every line. The renderer now makes that legible,
but the model should not be writing it for a phone in the first place.
"""

from __future__ import annotations

from types import SimpleNamespace

from robothor.engine.models import DeliveryMode, TriggerType
from robothor.engine.prompts import TELEGRAM_SURFACE_NOTE, with_surface_note


def _cfg(mode: DeliveryMode = DeliveryMode.NONE, channel: str = "") -> SimpleNamespace:
    return SimpleNamespace(delivery_mode=mode, delivery_channel=channel)


def test_a_telegram_turn_is_told_it_is_on_a_phone() -> None:
    turn = with_surface_note("CURRENT USER: the operator", TriggerType.TELEGRAM, _cfg())
    assert turn.startswith("CURRENT USER: the operator")
    assert TELEGRAM_SURFACE_NOTE in turn


def test_the_note_stands_alone_when_there_is_no_preamble() -> None:
    assert with_surface_note("", TriggerType.TELEGRAM, _cfg()) == TELEGRAM_SURFACE_NOTE


def test_webchat_is_not() -> None:
    assert with_surface_note("x", TriggerType.WEBCHAT, _cfg()) == "x"


def test_a_scheduled_run_announcing_to_telegram_is() -> None:
    cfg = _cfg(DeliveryMode.ANNOUNCE, "telegram")
    assert TELEGRAM_SURFACE_NOTE in with_surface_note("", TriggerType.CRON, cfg)


def test_a_scheduled_run_that_announces_nowhere_is_not() -> None:
    cfg = _cfg(DeliveryMode.NONE, "telegram")
    assert with_surface_note("", TriggerType.CRON, cfg) == ""


def test_the_note_names_the_rules_that_matter() -> None:
    note = TELEGRAM_SURFACE_NOTE.lower()
    for rule in ("phone", "table", "bold"):
        assert rule in note
