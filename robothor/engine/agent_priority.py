"""How a run competes for a slot on a contended device.

Priority is a property of the (trigger, agent) PAIR, not the agent. `main`'s
Telegram turn and `main`'s cron heartbeat are the same agent and must not
compete the same way: a person is waiting on one and nobody is waiting on the
other. Without that distinction "interactive Telegram runs while background
work defers" is not expressible at all.

Everything is derived. `docs/agents/*.yaml` is gitignored INSTANCE data, so a
platform default that depends on a manifest field ships broken to every fresh
instance; an optional `priority:` may override, but nothing requires it.

The obvious derivation is wrong, and it is worth recording why: leading with
`delivery.mode == "announce"` looks like "operator-facing", but `main` is
`delivery.mode: none` (main.yaml:17-18) — only its HEARTBEAT override announces.
That rule classifies the interactive agent as background and defers the one
thing that must never be deferred.
"""

from __future__ import annotations

from typing import Any

from robothor.engine.models import TriggerType
from robothor.engine.pool import Priority

#: A human is on the other end of these.
_INTERACTIVE_TRIGGERS = frozenset(
    {
        TriggerType.TELEGRAM,
        TriggerType.WEBCHAT,
        TriggerType.SLACK,
        TriggerType.IDE,
        TriggerType.MANUAL,
        TriggerType.CHANNEL_EVENT,
    }
)

_KNOWN = {p.value for p in Priority}


def classify(
    agent_id: str,
    trigger_type: TriggerType | None,
    agent_config: Any = None,
    engine_config: Any = None,
) -> Priority:
    """Priority for this (trigger, agent) pair.

    Fails OPEN to CRITICAL: an agent we cannot classify must keep running.
    Deferring work because a manifest failed to load would turn a config
    problem into an outage.
    """
    if trigger_type in _INTERACTIVE_TRIGGERS:
        return Priority.INTERACTIVE

    if agent_config is None:
        return Priority.CRITICAL

    declared = str(getattr(agent_config, "priority", "") or "").strip().lower()
    if declared in _KNOWN:
        return Priority(declared)

    if engine_config is not None:
        if agent_id and agent_id == getattr(engine_config, "default_chat_agent", None):
            return Priority.CRITICAL
        if agent_id in (getattr(engine_config, "required_agent_ids", None) or ()):
            return Priority.CRITICAL

    if str(getattr(agent_config, "department", "") or "").lower() == "core":
        return Priority.CRITICAL

    delivery = getattr(agent_config, "delivery_mode", None)
    if str(getattr(delivery, "value", delivery) or "").lower() == "announce":
        return Priority.CRITICAL

    return Priority.BACKGROUND
