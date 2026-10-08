"""The ``email`` and ``calendar`` event contract, written down.

For a long time the only definition of what an ``email.new`` event carries was
the instance script that publishes it. Any second producer (the Microsoft 365
ingest worker, :mod:`robothor.workspace.ingest`) has to publish exactly what
the consumers already read, so the contract lives here as JSON Schemas under
``robothor/events/schemas/``:

* ``email_new`` -- the payload of ``email.new`` on the ``email`` stream;
* ``calendar_event`` -- the payload of every ``calendar.*`` change event on the
  ``calendar`` stream (:data:`CALENDAR_EVENT_TYPES`);
* ``email_log`` -- the shape of ``email-log.json``, which the dashboards read.

The schemas name generic fields only. ``docs/event-bus.md`` documents them.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

__all__ = [
    "CALENDAR_CANCELLATION",
    "CALENDAR_EVENT_TYPES",
    "CALENDAR_MODIFIED",
    "CALENDAR_NEW",
    "CALENDAR_RESCHEDULED",
    "CALENDAR_STREAM",
    "EMAIL_NEW",
    "EMAIL_STREAM",
    "SCHEMA_NAMES",
    "ContractError",
    "email_new_payload",
    "schema",
    "validate",
]

EMAIL_STREAM = "email"
EMAIL_NEW = "email.new"

CALENDAR_STREAM = "calendar"
CALENDAR_NEW = "calendar.new"
CALENDAR_MODIFIED = "calendar.modified"
CALENDAR_RESCHEDULED = "calendar.rescheduled"
CALENDAR_CANCELLATION = "calendar.cancellation"

#: ``change_type`` in a calendar payload -> the event type it is published as.
#: ``calendar.new``/``.modified``/``.rescheduled`` trigger the calendar
#: pipeline (docs/workflows/calendar-pipeline.yaml); ``calendar.cancellation``
#: is what :mod:`robothor.events.consumers.calendar` handles.
CALENDAR_EVENT_TYPES: dict[str, str] = {
    "created": CALENDAR_NEW,
    "updated": CALENDAR_MODIFIED,
    "rescheduled": CALENDAR_RESCHEDULED,
    "cancelled": CALENDAR_CANCELLATION,
}

SCHEMA_NAMES = ("email_new", "calendar_event", "email_log")

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

#: The email-log.json entry fields an ``email.new`` payload repeats.
_EMAIL_NEW_FIELDS = ("id", "from", "subject", "date", "labels")


class ContractError(ValueError):
    """A payload does not match its schema. The message names the field, not the value."""


@cache
def schema(name: str) -> dict[str, Any]:
    """The JSON Schema ``name`` (one of :data:`SCHEMA_NAMES`)."""
    if name not in SCHEMA_NAMES:
        raise KeyError(f"unknown event schema {name!r}")
    loaded: dict[str, Any] = json.loads((_SCHEMA_DIR / f"{name}.json").read_text("utf-8"))
    return loaded


def validate(name: str, instance: Any) -> None:
    """Raise :class:`ContractError` unless ``instance`` matches schema ``name``."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema(name))
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise ContractError(f"{name}: {where}: {first.validator} check failed")


def email_new_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """The ``email.new`` payload for one email-log.json entry.

    The keys the Google sync has always sent, then ``threadId`` and
    ``provider`` when the entry has them.
    """
    payload: dict[str, Any] = {key: entry.get(key) for key in _EMAIL_NEW_FIELDS}
    payload["labels"] = list(entry.get("labels") or [])
    if entry.get("threadId"):
        payload["threadId"] = entry["threadId"]
    if entry.get("provider"):
        payload["provider"] = entry["provider"]
    return payload
