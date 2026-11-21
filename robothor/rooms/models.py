"""Rows of the rooms layer, as plain dataclasses. Ids are strings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date, datetime

#: A room's kind: a scheduled report, an extra thread with an agent, a CRM task.
KINDS = frozenset({"report", "agent", "task"})
#: The period an edition covers.
GRAINS = frozenset({"day", "week", "month", "custom"})
EDITION_KINDS = frozenset({"edition", "dashboard"})
TASK_ORIGINS = frozenset({"subject", "suggestion", "linked"})
#: The keys a room's ``settings`` may hold.
SETTINGS_KEYS = frozenset(
    {
        "tz",
        "default_grain",
        "schedule_text",
        "max_cost_usd",
        "max_on_demand_per_day",
        "notify_default",
    }
)
#: The dashboard components an edition's blocks may name.
ALLOWED_COMPONENTS = frozenset(
    {
        "render_metric_grid",
        "render_metric_card",
        "render_bar_chart",
        "render_line_chart",
        "render_pie_chart",
        "render_data_table",
        "render_markdown",
        "render_timeline",
    }
)


def room_link(room_id: str) -> str:
    """The dashboard link every result that names a room carries."""
    return f"/?v=chat&room={room_id}"


@dataclass(frozen=True)
class Room:
    tenant_id: str
    id: str
    kind: str
    title: str
    agent_id: str
    session_key: str
    created_by: str
    run_as_user_id: str | None = None
    instructions: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    paused: bool = False
    consecutive_failures: int = 0
    rev: int = 1
    last_activity_at: datetime | None = None
    last_message_id: int | None = None
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    @property
    def link(self) -> str:
        return room_link(self.id)

    def definition(self) -> dict[str, Any]:
        """What a revision stores: ``{title, instructions, agent_id, settings, schedule}``."""
        return {
            "title": self.title,
            "instructions": self.instructions,
            "agent_id": self.agent_id,
            "settings": dict(self.settings),
            "schedule": self.settings.get("schedule_text"),
        }


@dataclass(frozen=True)
class RoomSummary:
    tenant_id: str
    id: str
    kind: str
    title: str
    agent_id: str
    paused: bool
    archived: bool
    unread: int
    last_activity_at: datetime | None
    #: ``{id, seq, published_seq, title, period_start, period_end}`` of the latest published
    #: edition that is not a preview, or None.
    latest_edition: dict[str, Any] | None = None

    @property
    def link(self) -> str:
        return room_link(self.id)


@dataclass(frozen=True)
class RoomMember:
    tenant_id: str
    room_id: str
    user_id: str
    assigned: bool = False
    added_by: str | None = None
    last_read_message_id: int | None = None
    #: The ``published_seq`` of the last edition this member has seen.
    last_read_published_seq: int | None = None
    notify: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Revision:
    tenant_id: str
    room_id: str
    rev: int
    changed_by: str | None
    changed_at: datetime | None
    definition: dict[str, Any]


@dataclass(frozen=True)
class Edition:
    tenant_id: str
    id: str
    room_id: str
    seq: int
    kind: str
    status: str
    preview: bool
    period_start: date | None
    period_end: date | None
    grain: str | None
    revision: int
    #: Publish order within the room, set when a non-preview edition publishes
    #: (``seq`` is build order). Unread counts and read marks use this.
    published_seq: int | None = None
    title: str = ""
    summary: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    run_id: str | None = None
    chat_message_id: int | None = None
    requested_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def edition_json(edition: Edition) -> dict[str, Any]:
    """The contract's camelCase ``Edition``, as the engine and the bridge answer it."""
    return {
        "id": edition.id,
        "seq": edition.seq,
        "kind": edition.kind,
        "status": edition.status,
        "preview": edition.preview,
        "periodStart": _iso(edition.period_start),
        "periodEnd": _iso(edition.period_end),
        "grain": edition.grain,
        "revision": edition.revision,
        "title": edition.title,
        "summary": edition.summary,
        "blocks": list(edition.blocks),
        "suggestions": list(edition.suggestions),
        "provenance": dict(edition.provenance),
        "createdAt": _iso(edition.created_at),
    }


@dataclass(frozen=True)
class RoomTask:
    tenant_id: str
    room_id: str
    task_id: str
    edition_id: str | None = None
    origin: str = "linked"
    suggestion_key: str | None = None
    accepted_by: str | None = None
    dismissed_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def pending(self) -> bool:
        return self.accepted_by is None and self.dismissed_by is None
