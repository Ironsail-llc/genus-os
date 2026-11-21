"""A member's notification preferences for a workspace: the one shape.

Stored in ``room_members.notify`` as::

    {"in_app": bool, "muted": bool, "channels": {"<channel name>": bool}}

The bridge's ``PUT /api/rooms/{id}/notify`` writes it (:func:`stored_notify`)
and fan-out reads it (:func:`parse_notify`), so both sides import the shape
from here. A missing or empty value means the defaults: in-app on, not muted,
no external channel.

- ``muted``: nothing at all, on any surface.
- ``in_app`` false: no note in the main conversation and no inbox row.
- ``channels``: each name set true is used, when the person has that channel
  linked (fan-out checks the link at send time).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["NotifyPrefs", "parse_notify", "stored_notify"]


@dataclass(frozen=True)
class NotifyPrefs:
    in_app: bool = True
    muted: bool = False
    #: Channel names turned on, sorted.
    channels: tuple[str, ...] = ()

    @property
    def delivers_in_app(self) -> bool:
        return self.in_app and not self.muted

    @property
    def external_channels(self) -> tuple[str, ...]:
        return () if self.muted else self.channels


def parse_notify(raw: Any, *, default_muted: bool = False) -> NotifyPrefs:
    """The preferences ``raw`` (a stored ``notify``) describes. Anything not a
    mapping, or keys of the wrong type, read as their defaults. ``default_muted``
    applies when ``raw`` says nothing about ``muted`` (a workspace whose
    ``notify_default`` is false)."""
    data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    in_app = data.get("in_app")
    muted = data.get("muted")
    channels = data.get("channels")
    on = (
        sorted(str(k) for k, v in channels.items() if k and v is True)
        if isinstance(channels, Mapping)
        else []
    )
    return NotifyPrefs(
        in_app=in_app if isinstance(in_app, bool) else True,
        muted=muted if isinstance(muted, bool) else bool(default_muted),
        channels=tuple(on),
    )


def stored_notify(*, in_app: bool, muted: bool, channels: Mapping[str, bool]) -> dict[str, Any]:
    """The ``room_members.notify`` value for these preferences."""
    return {
        "in_app": bool(in_app),
        "muted": bool(muted),
        "channels": {str(k): bool(v) for k, v in channels.items()},
    }
