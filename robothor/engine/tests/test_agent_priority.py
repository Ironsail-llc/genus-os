"""Priority must be right for a fresh instance that configured nothing."""

from __future__ import annotations

from robothor.engine.agent_priority import classify
from robothor.engine.models import TriggerType
from robothor.engine.pool import Priority


class _Cfg:
    def __init__(self, **kw):
        self.priority = kw.get("priority", "")
        self.department = kw.get("department", "operations")
        self.delivery_mode = kw.get("delivery_mode", "none")


class _Engine:
    default_chat_agent = "main"
    required_agent_ids = ("main",)


def test_main_is_critical_despite_delivery_mode_none():
    """The trap: main.yaml:17-18 is `mode: none`. An announce-first rule
    classifies the interactive agent as background and defers it."""
    cfg = _Cfg(department="core", delivery_mode="none")
    assert classify("main", TriggerType.CRON, cfg, _Engine()) is Priority.CRITICAL


def test_a_telegram_turn_is_interactive_for_any_agent():
    assert classify("crm-dedup", TriggerType.TELEGRAM, _Cfg(), _Engine()) is Priority.INTERACTIVE


def test_mains_heartbeat_is_critical_not_interactive():
    """What makes 'interactive Telegram runs, main's heartbeat can wait'
    expressible at all."""
    cfg = _Cfg(department="core")
    assert classify("main", TriggerType.CRON, cfg, _Engine()) is Priority.CRITICAL


def test_a_background_cron_agent_is_background():
    """Without this the gate is inert — nothing would ever be deferred."""
    assert classify("crm-dedup", TriggerType.CRON, _Cfg(), _Engine()) is Priority.BACKGROUND


def test_an_unloadable_config_fails_open_to_critical():
    assert classify("whatever", TriggerType.CRON, None, _Engine()) is Priority.CRITICAL


def test_an_announce_agent_is_critical():
    assert (
        classify("morning-briefing", TriggerType.CRON, _Cfg(delivery_mode="announce"), _Engine())
        is Priority.CRITICAL
    )


def test_a_manifest_override_wins():
    assert (
        classify("x", TriggerType.CRON, _Cfg(priority="background"), _Engine())
        is Priority.BACKGROUND
    )


def test_a_fresh_instance_with_no_engine_config_still_works():
    assert classify("main", TriggerType.CRON, _Cfg(), None) is Priority.BACKGROUND
    assert classify("main", TriggerType.TELEGRAM, _Cfg(), None) is Priority.INTERACTIVE
