"""Data access for rooms. Sync; async callers use ``asyncio.to_thread``.

Every function takes the tenant explicitly and filters by it, so another
tenant's ids return nothing even on a connection row-level security does not
bind. Reads of a malformed id return nothing; writes against a room that is
not in the tenant raise ``RoomNotFound``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json, RealDictCursor

from robothor.db.connection import get_connection
from robothor.rooms.errors import InvalidBlocks, RevisionConflict, RoomNotFound
from robothor.rooms.keys import canonical_uuid, room_session_key
from robothor.rooms.models import (
    ALLOWED_COMPONENTS,
    EDITION_KINDS,
    GRAINS,
    KINDS,
    SETTINGS_KEYS,
    TASK_ORIGINS,
    Edition,
    Revision,
    Room,
    RoomMember,
    RoomSummary,
    RoomTask,
)

if TYPE_CHECKING:
    from datetime import date

__all__ = [
    "was_removed",
    "task_history",
    "take_task",
    "rooms_linked_to_task",
    "room_task_views",
    "room_task_view",
    "join_task_room",
    "get_task_room",
    "get_crm_task",
    "ensure_task_room",
    "dismiss_suggestion",
    "create_suggested_tasks",
    "accept_task",
    "add_member",
    "archive_room",
    "create_edition",
    "create_room",
    "dismiss_task",
    "duplicate_room",
    "fail_edition",
    "find_edition_for_period",
    "get_edition",
    "get_member",
    "get_or_create_task_room",
    "get_room",
    "link_task",
    "list_all_rooms",
    "list_editions",
    "list_members",
    "list_revisions",
    "list_room_tasks",
    "list_rooms_for_user",
    "mark_read",
    "publish_edition",
    "record_activity",
    "recent_messages",
    "remove_member",
    "restore_revision",
    "set_assigned",
    "set_notify",
    "set_paused",
    "set_run_as",
    "unarchive_room",
    "update_definition",
]

#: The keys ``update_definition`` accepts. ``agent_id`` is fixed at creation:
#: it is part of the session key, so changing it would orphan the history.
_CHANGE_KEYS = frozenset({"title", "instructions", "settings", "schedule"})


# ─── row mapping ─────────────────────────────────────────────────────────


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def _room(row: Mapping[str, Any]) -> Room:
    return Room(
        tenant_id=row["tenant_id"],
        id=str(row["id"]),
        kind=row["kind"],
        title=row["title"],
        agent_id=row["agent_id"],
        session_key=row["session_key"],
        created_by=str(row["created_by"]),
        run_as_user_id=_s(row["run_as_user_id"]),
        instructions=row["instructions"],
        settings=dict(row["settings"] or {}),
        task_id=_s(row["task_id"]),
        paused=bool(row["paused"]),
        consecutive_failures=int(row["consecutive_failures"]),
        rev=int(row["rev"]),
        last_activity_at=row["last_activity_at"],
        last_message_id=row["last_message_id"],
        archived_at=row["archived_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _member(row: Mapping[str, Any]) -> RoomMember:
    return RoomMember(
        tenant_id=row["tenant_id"],
        room_id=str(row["room_id"]),
        user_id=str(row["user_id"]),
        assigned=bool(row["assigned"]),
        added_by=_s(row["added_by"]),
        last_read_message_id=row["last_read_message_id"],
        last_read_published_seq=row["last_read_published_seq"],
        notify=dict(row["notify"] or {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _revision(row: Mapping[str, Any]) -> Revision:
    return Revision(
        tenant_id=row["tenant_id"],
        room_id=str(row["room_id"]),
        rev=int(row["rev"]),
        changed_by=_s(row["changed_by"]),
        changed_at=row["changed_at"],
        definition=dict(row["definition"] or {}),
    )


def _edition(row: Mapping[str, Any]) -> Edition:
    return Edition(
        tenant_id=row["tenant_id"],
        id=str(row["id"]),
        room_id=str(row["room_id"]),
        seq=int(row["seq"]),
        kind=row["kind"],
        status=row["status"],
        preview=bool(row["preview"]),
        period_start=row["period_start"],
        period_end=row["period_end"],
        grain=row["grain"],
        revision=int(row["revision"]),
        published_seq=row["published_seq"],
        title=row["title"],
        summary=row["summary"],
        blocks=list(row["blocks"] or []),
        suggestions=list(row["suggestions"] or []),
        provenance=dict(row["provenance"] or {}),
        error=row["error"],
        run_id=row["run_id"],
        chat_message_id=row["chat_message_id"],
        requested_by=_s(row["requested_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _task(row: Mapping[str, Any]) -> RoomTask:
    return RoomTask(
        tenant_id=row["tenant_id"],
        room_id=str(row["room_id"]),
        task_id=str(row["task_id"]),
        edition_id=_s(row["edition_id"]),
        origin=row["origin"],
        suggestion_key=row["suggestion_key"],
        accepted_by=_s(row["accepted_by"]),
        dismissed_by=_s(row["dismissed_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _summary(row: Mapping[str, Any]) -> RoomSummary:
    latest = None
    if row["le_id"] is not None:
        latest = {
            "id": str(row["le_id"]),
            "seq": int(row["le_seq"]),
            "published_seq": int(row["le_published_seq"]),
            "title": row["le_title"],
            "period_start": row["le_start"],
            "period_end": row["le_end"],
        }
    return RoomSummary(
        tenant_id=row["tenant_id"],
        id=str(row["id"]),
        kind=row["kind"],
        title=row["title"],
        agent_id=row["agent_id"],
        paused=bool(row["paused"]),
        archived=row["archived_at"] is not None,
        unread=int(row["unread"]),
        last_activity_at=row["last_activity_at"],
        latest_edition=latest,
    )


# ─── validation ──────────────────────────────────────────────────────────


def _required_uuid(value: Any, what: str) -> str:
    canonical = canonical_uuid(value)
    if canonical is None:
        raise ValueError(f"{what} must be a UUID, got {value!r}")
    return canonical


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a room needs a title")
    return value.strip()


def _settings(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("settings must be an object")
    unknown = sorted(set(value) - SETTINGS_KEYS)
    if unknown:
        raise ValueError(f"unknown room settings: {', '.join(unknown)}")
    grain = value.get("default_grain")
    if grain is not None and grain not in GRAINS:
        raise ValueError(f"default_grain must be one of {sorted(GRAINS)}")
    tz = value.get("tz")
    if tz is not None:
        try:
            ZoneInfo(str(tz))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {tz!r}") from exc
    return dict(value)


def _blocks(blocks: Any, suggestions: Any) -> None:
    if not isinstance(blocks, list):
        raise InvalidBlocks("blocks must be a list")
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or set(block) != {"component", "props"}:
            raise InvalidBlocks(f"block {i} must be exactly {{component, props}}")
        if block["component"] not in ALLOWED_COMPONENTS:
            raise InvalidBlocks(f"block {i}: component {block['component']!r} is not allowed")
        if not isinstance(block["props"], dict):
            raise InvalidBlocks(f"block {i}: props must be an object")
    if not isinstance(suggestions, list) or not all(isinstance(s, dict) for s in suggestions):
        raise InvalidBlocks("suggestions must be a list of objects")


# ─── rooms ───────────────────────────────────────────────────────────────


def _insert_room(
    cur: Any,
    tenant_id: str,
    *,
    kind: str,
    title: str,
    agent_id: str,
    created_by: str,
    instructions: str,
    settings: dict[str, Any],
    task_id: str | None,
    run_as_user_id: str | None,
) -> Room | None:
    """Insert a room, its revision 1 and its creator as assigned, in ``cur``'s
    transaction. None when a room for ``task_id`` already exists."""
    room_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by,
                              run_as_user_id, instructions, settings, task_id, last_activity_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (tenant_id, task_id) DO NOTHING
           RETURNING *""",
        (
            tenant_id,
            room_id,
            kind,
            title,
            agent_id,
            room_session_key(agent_id, room_id),
            created_by,
            run_as_user_id or created_by,
            instructions,
            Json(settings),
            task_id,
        ),
    )
    row = cur.fetchone()
    if row is None:
        return None
    room = _room(row)
    _write_revision(cur, room, created_by)
    cur.execute(
        "INSERT INTO room_members (tenant_id, room_id, user_id, assigned, added_by) "
        "VALUES (%s, %s, %s, TRUE, %s)",
        (tenant_id, room.id, created_by, created_by),
    )
    return room


def _write_revision(cur: Any, room: Room, changed_by: str | None) -> None:
    cur.execute(
        "INSERT INTO room_revisions (tenant_id, room_id, rev, changed_by, definition) "
        "VALUES (%s, %s, %s, %s, %s)",
        (room.tenant_id, room.id, room.rev, changed_by, Json(room.definition())),
    )


def create_room(
    tenant_id: str,
    *,
    kind: str,
    title: str,
    agent_id: str,
    created_by: str,
    instructions: str = "",
    settings: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    run_as_user_id: str | None = None,
) -> Room:
    """A new room: revision 1 written and the creator a member, ``assigned``."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    room_session_key(agent_id, str(uuid.uuid4()))  # refuses an agent id that would not parse
    creator = _required_uuid(created_by, "created_by")
    task = _required_uuid(task_id, "task_id") if task_id else None
    run_as = _required_uuid(run_as_user_id, "run_as_user_id") if run_as_user_id else None
    clean_title, clean_settings = _title(title), _settings(settings)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        room = _insert_room(
            cur,
            tenant_id,
            kind=kind,
            title=clean_title,
            agent_id=agent_id,
            created_by=creator,
            instructions=instructions or "",
            settings=clean_settings,
            task_id=task,
            run_as_user_id=run_as,
        )
        if room is None:
            raise ValueError(f"task {task_id} already has a room")
        conn.commit()
        return room


def get_room(tenant_id: str, room_id: str) -> Room | None:
    canonical = canonical_uuid(room_id)
    if canonical is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM rooms WHERE tenant_id = %s AND id = %s", (tenant_id, canonical))
        row = cur.fetchone()
        return _room(row) if row else None


def _locked_room(cur: Any, tenant_id: str, room_id: str) -> Room:
    canonical = canonical_uuid(room_id)
    row = None
    if canonical is not None:
        cur.execute(
            "SELECT * FROM rooms WHERE tenant_id = %s AND id = %s FOR UPDATE",
            (tenant_id, canonical),
        )
        row = cur.fetchone()
    if row is None:
        raise RoomNotFound(room_id)
    return _room(row)


_SUMMARY_SELECT = """
SELECT r.tenant_id, r.id, r.kind, r.title, r.agent_id, r.paused, r.archived_at,
       r.last_activity_at, r.created_at,
       {unread} AS unread,
       le.id AS le_id, le.seq AS le_seq, le.published_seq AS le_published_seq,
       le.title AS le_title,
       le.period_start AS le_start, le.period_end AS le_end
FROM {source}
LEFT JOIN LATERAL (
    SELECT e.id, e.seq, e.published_seq, e.title, e.period_start, e.period_end
    FROM room_editions e
    WHERE e.tenant_id = r.tenant_id AND e.room_id = r.id
      AND e.published_seq IS NOT NULL
    ORDER BY e.published_seq DESC LIMIT 1
) le ON TRUE
{extra}
WHERE r.tenant_id = %(tenant)s AND (%(archived)s OR r.archived_at IS NULL) {where}
ORDER BY r.last_activity_at DESC NULLS LAST, r.created_at DESC
"""

#: Per member: messages after their read mark, plus published editions after
#: their edition mark that did not arrive as a chat message. Editions count by
#: publish order (``published_seq``, never set on a preview), so one published
#: after a later-built one is still unread.
_UNREAD_JOINS = """
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n FROM chat_sessions s JOIN chat_messages cm ON cm.session_id = s.id
    WHERE s.tenant_id = r.tenant_id AND s.session_key = r.session_key
      AND cm.id > COALESCE(m.last_read_message_id, 0)
) msg ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n FROM room_editions e
    WHERE e.tenant_id = r.tenant_id AND e.room_id = r.id AND e.chat_message_id IS NULL
      AND e.published_seq > COALESCE(m.last_read_published_seq, 0)
) ed ON TRUE
"""


def list_rooms_for_user(
    tenant_id: str, user_id: str, *, include_archived: bool = False
) -> list[RoomSummary]:
    """The rooms ``user_id`` is a member of, most recently active first, each
    with that user's unread count. One query, however many rooms."""
    user = canonical_uuid(user_id)
    if user is None:
        return []
    query = _SUMMARY_SELECT.format(
        unread="msg.n + ed.n",
        source="room_members m JOIN rooms r ON r.id = m.room_id AND r.tenant_id = m.tenant_id",
        extra=_UNREAD_JOINS,
        where="AND m.user_id = %(user)s",
    )
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, {"tenant": tenant_id, "user": user, "archived": include_archived})
        return [_summary(r) for r in cur.fetchall()]


def list_all_rooms(tenant_id: str, *, include_archived: bool = False) -> list[RoomSummary]:
    """Every room of the tenant (an admin's view). ``unread`` is 0: there is no reader."""
    query = _SUMMARY_SELECT.format(unread="0", source="rooms r", extra="", where="")
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, {"tenant": tenant_id, "archived": include_archived})
        return [_summary(r) for r in cur.fetchall()]


def _save_definition(
    cur: Any,
    room: Room,
    *,
    title: str,
    instructions: str,
    settings: dict[str, Any],
    changed_by: str | None,
) -> Room:
    cur.execute(
        """UPDATE rooms SET title = %s, instructions = %s, settings = %s, rev = rev + 1,
                  updated_at = now()
           WHERE tenant_id = %s AND id = %s RETURNING *""",
        (title, instructions, Json(settings), room.tenant_id, room.id),
    )
    saved = _room(cur.fetchone())
    _write_revision(cur, saved, changed_by)
    return saved


def update_definition(
    tenant_id: str,
    room_id: str,
    changes: Mapping[str, Any],
    changed_by: str,
    expected_rev: int,
) -> Room:
    """Apply ``changes`` (``title``, ``instructions``, ``settings``, ``schedule``)
    when the room is still at ``expected_rev``: the next revision is written and
    ``rooms.rev`` bumped in one transaction. ``settings`` merges into the current
    settings; a ``None`` value removes the key. ``schedule`` sets
    ``settings.schedule_text``. Raises ``RevisionConflict`` on a stale revision."""
    if not changes:
        raise ValueError("nothing to change")
    if "agent_id" in changes:
        raise ValueError("a room's agent is fixed; duplicate the room to use another agent")
    unknown = sorted(set(changes) - _CHANGE_KEYS)
    if unknown:
        raise ValueError(f"cannot change {', '.join(unknown)}")
    if "settings" in changes and not isinstance(changes["settings"], Mapping):
        raise ValueError("settings must be an object")
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        room = _locked_room(cur, tenant_id, room_id)
        if room.rev != expected_rev:
            raise RevisionConflict(room.rev)
        settings = dict(room.settings)
        for key, value in dict(changes.get("settings") or {}).items():
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value
        if "schedule" in changes:
            if changes["schedule"] is None:
                settings.pop("schedule_text", None)
            else:
                settings["schedule_text"] = str(changes["schedule"])
        saved = _save_definition(
            cur,
            room,
            title=_title(changes["title"]) if "title" in changes else room.title,
            instructions=str(changes.get("instructions", room.instructions) or ""),
            settings=_settings(settings),
            changed_by=changed_by,
        )
        conn.commit()
        return saved


def list_revisions(tenant_id: str, room_id: str) -> list[Revision]:
    """Newest first."""
    canonical = canonical_uuid(room_id)
    if canonical is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM room_revisions WHERE tenant_id = %s AND room_id = %s ORDER BY rev DESC",
            (tenant_id, canonical),
        )
        return [_revision(r) for r in cur.fetchall()]


def restore_revision(
    tenant_id: str,
    room_id: str,
    rev: int,
    restored_by: str,
    *,
    expected_rev: int | None = None,
) -> Room:
    """Write a NEW revision carrying revision ``rev``'s definition."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        room = _locked_room(cur, tenant_id, room_id)
        if expected_rev is not None and room.rev != expected_rev:
            raise RevisionConflict(room.rev)
        cur.execute(
            "SELECT definition FROM room_revisions WHERE tenant_id = %s AND room_id = %s "
            "AND rev = %s",
            (tenant_id, room.id, rev),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"the room has no revision {rev}")
        definition = dict(row["definition"] or {})
        saved = _save_definition(
            cur,
            room,
            title=_title(definition.get("title", room.title)),
            instructions=str(definition.get("instructions") or ""),
            settings=_settings(definition.get("settings") or {}),
            changed_by=restored_by,
        )
        conn.commit()
        return saved


def duplicate_room(
    tenant_id: str, room_id: str, created_by: str, *, title: str | None = None
) -> Room:
    """A new room with this room's definition only: no members but the creator
    (assigned), no editions, no tasks. A task room's copy is an agent room."""
    creator = _required_uuid(created_by, "created_by")
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        source = _locked_room(cur, tenant_id, room_id)
        room = _insert_room(
            cur,
            tenant_id,
            kind="agent" if source.kind == "task" else source.kind,
            title=_title(title) if title is not None else f"{source.title} (copy)",
            agent_id=source.agent_id,
            created_by=creator,
            instructions=source.instructions,
            settings=dict(source.settings),
            task_id=None,
            run_as_user_id=None,
        )
        assert room is not None  # no task id, so no conflict
        conn.commit()
        return room


def _set_archived(tenant_id: str, room_id: str, archived: bool) -> Room | None:
    canonical = canonical_uuid(room_id)
    if canonical is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "UPDATE rooms SET archived_at = CASE WHEN %s THEN COALESCE(archived_at, now()) "
            "ELSE NULL END, updated_at = now() WHERE tenant_id = %s AND id = %s RETURNING *",
            (archived, tenant_id, canonical),
        )
        row = cur.fetchone()
        conn.commit()
        return _room(row) if row else None


def archive_room(tenant_id: str, room_id: str) -> Room | None:
    return _set_archived(tenant_id, room_id, True)


def unarchive_room(tenant_id: str, room_id: str) -> Room | None:
    return _set_archived(tenant_id, room_id, False)


def record_activity(tenant_id: str, room_id: str, message_id: int) -> Room | None:
    """Mark a saved message: ``last_activity_at`` now, ``last_message_id`` the newest.

    The message mark only moves forward. None when the room is not in the tenant.
    """
    canonical = canonical_uuid(room_id)
    if canonical is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "UPDATE rooms SET last_activity_at = now(), updated_at = now(), "
            "last_message_id = GREATEST(COALESCE(last_message_id, 0), %s) "
            "WHERE tenant_id = %s AND id = %s RETURNING *",
            (int(message_id), tenant_id, canonical),
        )
        row = cur.fetchone()
        conn.commit()
        return _room(row) if row else None


def _set_room_column(tenant_id: str, room_id: str, assignment: str, value: Any) -> Room:
    canonical = canonical_uuid(room_id)
    row = None
    if canonical is not None:
        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(
                # ``assignment`` is one of two constants below, never caller input.
                f"UPDATE rooms SET {assignment}, updated_at = now() "  # noqa: S608
                "WHERE tenant_id = %s AND id = %s RETURNING *",
                (value, tenant_id, canonical),
            )
            row = cur.fetchone()
            conn.commit()
    if row is None:
        raise RoomNotFound(room_id)
    return _room(row)


def set_paused(tenant_id: str, room_id: str, paused: bool) -> Room:
    """Pause or resume a room. Not part of the definition: no revision is written."""
    return _set_room_column(tenant_id, room_id, "paused = %s", bool(paused))


def set_run_as(tenant_id: str, room_id: str, user_id: str) -> Room:
    """Change whose permissions a room's scheduled runs use (reassignment)."""
    return _set_room_column(
        tenant_id, room_id, "run_as_user_id = %s", _required_uuid(user_id, "user_id")
    )


def _text(content: Any) -> str:
    """A chat message's text: a string, or the text parts of a content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def recent_messages(tenant_id: str, room_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """The room's last ``limit`` user and assistant messages, oldest first:
    ``{id, role, content, author_user_id, created_at}``. Tool messages are skipped."""
    canonical = canonical_uuid(room_id)
    if canonical is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT cm.id, cm.message, cm.created_at FROM rooms r
               JOIN chat_sessions s ON s.tenant_id = r.tenant_id AND s.session_key = r.session_key
               JOIN chat_messages cm ON cm.session_id = s.id
               WHERE r.tenant_id = %s AND r.id = %s
                 AND cm.message->>'role' IN ('user', 'assistant')
               ORDER BY cm.id DESC LIMIT %s""",
            (tenant_id, canonical, max(1, int(limit))),
        )
        rows = cur.fetchall()
    out = []
    for row in reversed(rows):
        message = dict(row["message"] or {})
        author = message.get("author_user_id") or message.get("authorUserId")
        out.append(
            {
                "id": int(row["id"]),
                "role": message.get("role"),
                "content": _text(message.get("content")),
                "author_user_id": canonical_uuid(author),
                "created_at": row["created_at"],
            }
        )
    return out


# ─── members ─────────────────────────────────────────────────────────────


def _room_exists(cur: Any, tenant_id: str, room_id: str) -> str:
    canonical = canonical_uuid(room_id)
    if canonical is not None:
        cur.execute("SELECT 1 FROM rooms WHERE tenant_id = %s AND id = %s", (tenant_id, canonical))
        if cur.fetchone():
            return canonical
    raise RoomNotFound(room_id)


def get_member(tenant_id: str, room_id: str, user_id: str) -> RoomMember | None:
    room, user = canonical_uuid(room_id), canonical_uuid(user_id)
    if room is None or user is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM room_members WHERE tenant_id = %s AND room_id = %s AND user_id = %s",
            (tenant_id, room, user),
        )
        row = cur.fetchone()
        return _member(row) if row else None


def add_member(
    tenant_id: str, room_id: str, user_id: str, *, added_by: str, assigned: bool = False
) -> RoomMember:
    """Add (or re-add, setting ``assigned``) a member."""
    user = _required_uuid(user_id, "user_id")
    adder = _required_uuid(added_by, "added_by")
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        room = _room_exists(cur, tenant_id, room_id)
        cur.execute(
            """INSERT INTO room_members (tenant_id, room_id, user_id, assigned, added_by)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, room_id, user_id)
               DO UPDATE SET assigned = EXCLUDED.assigned, updated_at = now()
               RETURNING *""",
            (tenant_id, room, user, assigned, adder),
        )
        member = _member(cur.fetchone())
        # Re-added by someone who can share the room: the removal is over.
        cur.execute(
            "DELETE FROM room_member_removals WHERE tenant_id = %s AND room_id = %s AND user_id = %s",
            (tenant_id, room, user),
        )
        conn.commit()
        return member


def _update_member(
    tenant_id: str, room_id: str, user_id: str, assignments: str, params: tuple[Any, ...]
) -> RoomMember | None:
    room, user = canonical_uuid(room_id), canonical_uuid(user_id)
    if room is None or user is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            # ``assignments`` is a constant of this module, never caller input.
            f"UPDATE room_members m SET {assignments}, updated_at = now() "  # noqa: S608
            "WHERE m.tenant_id = %s AND m.room_id = %s AND m.user_id = %s RETURNING *",
            (*params, tenant_id, room, user),
        )
        row = cur.fetchone()
        conn.commit()
        return _member(row) if row else None


def remove_member(
    tenant_id: str, room_id: str, user_id: str, *, removed_by: str | None = None
) -> bool:
    """Remove a member and remember it (``room_member_removals``), so opening
    the room's task never re-adds them; only ``add_member`` (someone who can
    share the room) does."""
    room, user = canonical_uuid(room_id), canonical_uuid(user_id)
    if room is None or user is None:
        return False
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM room_members WHERE tenant_id = %s AND room_id = %s AND user_id = %s",
            (tenant_id, room, user),
        )
        removed = bool(cur.rowcount)
        if removed:
            cur.execute(
                """INSERT INTO room_member_removals (tenant_id, room_id, user_id, removed_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (tenant_id, room_id, user_id) DO UPDATE
                   SET removed_by = EXCLUDED.removed_by, removed_at = now(), updated_at = now()""",
                (tenant_id, room, user, canonical_uuid(removed_by)),
            )
        conn.commit()
        return removed


def was_removed(tenant_id: str, room_id: str, user_id: str) -> bool:
    """Whether ``user_id`` was removed from the room and not re-added since."""
    room, user = canonical_uuid(room_id), canonical_uuid(user_id)
    if room is None or user is None:
        return False
    with get_connection(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM room_member_removals "
            "WHERE tenant_id = %s AND room_id = %s AND user_id = %s",
            (tenant_id, room, user),
        )
        return cur.fetchone() is not None


def set_assigned(tenant_id: str, room_id: str, user_id: str, assigned: bool) -> RoomMember | None:
    """None when ``user_id`` is not a member."""
    return _update_member(tenant_id, room_id, user_id, "assigned = %s", (assigned,))


def set_notify(
    tenant_id: str, room_id: str, user_id: str, notify: Mapping[str, Any]
) -> RoomMember | None:
    """None when ``user_id`` is not a member."""
    return _update_member(tenant_id, room_id, user_id, "notify = %s", (Json(dict(notify)),))


def list_members(tenant_id: str, room_id: str) -> list[RoomMember]:
    """Assigned members first, then by when they were added."""
    room = canonical_uuid(room_id)
    if room is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM room_members WHERE tenant_id = %s AND room_id = %s "
            "ORDER BY assigned DESC, created_at, user_id",
            (tenant_id, room),
        )
        return [_member(r) for r in cur.fetchall()]


_LATEST_MESSAGE = """(SELECT MAX(cm.id) FROM rooms r
    JOIN chat_sessions s ON s.tenant_id = r.tenant_id AND s.session_key = r.session_key
    JOIN chat_messages cm ON cm.session_id = s.id
    WHERE r.tenant_id = m.tenant_id AND r.id = m.room_id)"""
_LATEST_EDITION = """(SELECT MAX(e.published_seq) FROM room_editions e
    WHERE e.tenant_id = m.tenant_id AND e.room_id = m.room_id)"""


def mark_read(
    tenant_id: str,
    room_id: str,
    user_id: str,
    *,
    message_id: int | None = None,
    published_seq: int | None = None,
) -> RoomMember | None:
    """Move the member's read marks forward (never back). ``published_seq`` is
    an edition's publish number (``Edition.published_seq``), not its build
    ``seq``. With neither mark given, both move to the latest message and
    published edition; previews never move a mark. None when not a member."""
    if message_id is None and published_seq is None:
        return _update_member(
            tenant_id,
            room_id,
            user_id,
            f"last_read_message_id = GREATEST(m.last_read_message_id, {_LATEST_MESSAGE}), "
            f"last_read_published_seq = GREATEST(m.last_read_published_seq, {_LATEST_EDITION})",
            (),
        )
    return _update_member(
        tenant_id,
        room_id,
        user_id,
        "last_read_message_id = GREATEST(m.last_read_message_id, %s), "
        "last_read_published_seq = GREATEST(m.last_read_published_seq, %s)",
        (message_id, published_seq),
    )


# ─── editions ────────────────────────────────────────────────────────────


def create_edition(
    tenant_id: str,
    room_id: str,
    *,
    kind: str = "edition",
    preview: bool = False,
    period_start: date | None = None,
    period_end: date | None = None,
    grain: str | None = None,
    requested_by: str | None = None,
    run_id: str | None = None,
    title: str = "",
) -> tuple[Edition, bool]:
    """A ``building`` edition with the room's next seq and current revision.

    ``(edition, created)``. For a non-preview request naming a grain and a
    period, an edition already building for that period is returned instead
    (``created`` False): concurrent requests share one build. Previews are
    never shared.
    """
    if kind not in EDITION_KINDS:
        raise ValueError(f"kind must be one of {sorted(EDITION_KINDS)}")
    if grain is not None and grain not in GRAINS:
        raise ValueError(f"grain must be one of {sorted(GRAINS)}")
    if (period_start is None) != (period_end is None):
        raise ValueError("a period needs both a start and an end")
    if period_start is not None and period_end is not None and period_start > period_end:
        raise ValueError("a period cannot end before it starts")
    requester = _required_uuid(requested_by, "requested_by") if requested_by else None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # The room row lock serialises seq allocation and the period check.
        room = _locked_room(cur, tenant_id, room_id)
        if not preview and grain is not None and period_start is not None:
            cur.execute(
                "SELECT * FROM room_editions WHERE tenant_id = %s AND room_id = %s "
                "AND grain = %s AND period_start = %s AND period_end = %s "
                "AND status = 'building' AND NOT preview",
                (tenant_id, room.id, grain, period_start, period_end),
            )
            existing = cur.fetchone()
            if existing:
                return _edition(existing), False
        cur.execute(
            """INSERT INTO room_editions (tenant_id, id, room_id, seq, kind, status, preview,
                   period_start, period_end, grain, revision, title, run_id, requested_by)
               SELECT %s, %s, %s, COALESCE(MAX(seq), 0) + 1, %s, 'building', %s, %s, %s, %s,
                      %s, %s, %s, %s
               FROM room_editions WHERE tenant_id = %s AND room_id = %s
               RETURNING *""",
            (
                tenant_id,
                str(uuid.uuid4()),
                room.id,
                kind,
                preview,
                period_start,
                period_end,
                grain,
                room.rev,
                title or "",
                run_id,
                requester,
                tenant_id,
                room.id,
            ),
        )
        edition = _edition(cur.fetchone())
        conn.commit()
        return edition, True


def publish_edition(
    tenant_id: str,
    edition_id: str,
    *,
    title: str,
    summary: str,
    blocks: list[dict[str, Any]],
    suggestions: list[dict[str, Any]] | None = None,
    provenance: Mapping[str, Any] | None = None,
    chat_message_id: int | None = None,
) -> Edition | None:
    """Publish a ``building`` edition. Raises ``InvalidBlocks`` for content
    outside the allowed components; None when there is no such building edition.

    ``revision`` becomes the edition's revision within its period: 1, or one
    more than the non-preview editions already published for that grain and
    period. An edition with no period is revision 1. (While building, the row
    carries the room's definition revision; the builder records that in
    provenance as ``room_rev``.)"""
    suggestions = [] if suggestions is None else suggestions
    _blocks(blocks, suggestions)
    canonical = canonical_uuid(edition_id)
    if canonical is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT room_id FROM room_editions WHERE tenant_id = %s AND id = %s",
            (tenant_id, canonical),
        )
        found = cur.fetchone()
        if found is None:
            return None
        # The room row lock serialises publish numbers (and orders this lock
        # before the edition row's, as create_edition does).
        _locked_room(cur, tenant_id, str(found["room_id"]))
        cur.execute(
            """UPDATE room_editions e SET status = 'published', title = %s, summary = %s,
                      blocks = %s, suggestions = %s, provenance = %s, chat_message_id = %s,
                      published_seq = CASE WHEN e.preview THEN NULL ELSE (
                          SELECT COALESCE(MAX(p.published_seq), 0) + 1 FROM room_editions p
                          WHERE p.tenant_id = e.tenant_id AND p.room_id = e.room_id) END,
                      revision = CASE WHEN e.period_start IS NULL THEN 1 ELSE 1 + (
                          SELECT COUNT(*) FROM room_editions p
                          WHERE p.tenant_id = e.tenant_id AND p.room_id = e.room_id
                            AND p.status = 'published' AND NOT p.preview
                            AND p.grain IS NOT DISTINCT FROM e.grain
                            AND p.period_start = e.period_start AND p.period_end = e.period_end
                      ) END,
                      updated_at = now()
               WHERE e.tenant_id = %s AND e.id = %s AND e.status = 'building' RETURNING *""",
            (
                title or "",
                summary or "",
                Json(blocks),
                Json(suggestions),
                Json(dict(provenance or {})),
                chat_message_id,
                tenant_id,
                canonical,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return None
        edition = _edition(row)
        if not edition.preview:
            cur.execute(
                "UPDATE rooms SET last_activity_at = now() WHERE tenant_id = %s AND id = %s",
                (tenant_id, edition.room_id),
            )
        conn.commit()
        return edition


def fail_edition(tenant_id: str, edition_id: str, error: str) -> Edition | None:
    """Mark a ``building`` edition failed. None when there is no such edition."""
    canonical = canonical_uuid(edition_id)
    if canonical is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "UPDATE room_editions SET status = 'failed', error = %s, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = 'building' RETURNING *",
            (error, tenant_id, canonical),
        )
        row = cur.fetchone()
        conn.commit()
        return _edition(row) if row else None


#: A preview is visible to its requester only; ``viewer_id`` None sees none.
_VISIBLE = "(NOT preview OR (%(viewer)s::uuid IS NOT NULL AND requested_by = %(viewer)s::uuid))"


def get_edition(tenant_id: str, edition_id: str, *, viewer_id: str | None) -> Edition | None:
    """One edition. A preview only when ``viewer_id`` requested it."""
    canonical = canonical_uuid(edition_id)
    if canonical is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT * FROM room_editions WHERE tenant_id = %(tenant)s AND id = %(id)s "  # noqa: S608
            f"AND {_VISIBLE}",
            {"tenant": tenant_id, "id": canonical, "viewer": canonical_uuid(viewer_id)},
        )
        row = cur.fetchone()
        return _edition(row) if row else None


def list_editions(
    tenant_id: str,
    room_id: str,
    *,
    viewer_id: str | None,
    include_unpublished: bool = False,
    limit: int = 50,
) -> list[Edition]:
    """Newest first: published ones (all, with ``include_unpublished``), and
    previews only ``viewer_id`` requested."""
    room = canonical_uuid(room_id)
    if room is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM room_editions WHERE tenant_id = %(tenant)s AND room_id = %(room)s "  # noqa: S608
            f"AND (%(all)s OR status = 'published') AND {_VISIBLE} "
            "ORDER BY seq DESC LIMIT %(limit)s",
            {
                "tenant": tenant_id,
                "room": room,
                "all": include_unpublished,
                "viewer": canonical_uuid(viewer_id),
                "limit": max(1, int(limit)),
            },
        )
        return [_edition(r) for r in cur.fetchall()]


def find_edition_for_period(
    tenant_id: str, room_id: str, grain: str, period_start: date, period_end: date
) -> Edition | None:
    """The newest published edition for the period, else the one building. Never a preview."""
    room = canonical_uuid(room_id)
    if room is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM room_editions WHERE tenant_id = %s AND room_id = %s AND grain = %s "
            "AND period_start = %s AND period_end = %s AND NOT preview "
            "AND status IN ('published', 'building') "
            "ORDER BY (status = 'published') DESC, published_seq DESC NULLS LAST, seq DESC "
            "LIMIT 1",
            (tenant_id, room, grain, period_start, period_end),
        )
        row = cur.fetchone()
        return _edition(row) if row else None


# ─── tasks ───────────────────────────────────────────────────────────────


def link_task(
    tenant_id: str,
    room_id: str,
    task_id: str,
    *,
    origin: str = "linked",
    edition_id: str | None = None,
    suggestion_key: str | None = None,
) -> RoomTask:
    """Link a CRM task of the tenant to the room. Idempotent: an existing link
    is returned unchanged."""
    if origin not in TASK_ORIGINS:
        raise ValueError(f"origin must be one of {sorted(TASK_ORIGINS)}")
    edition = _required_uuid(edition_id, "edition_id") if edition_id else None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        room = _room_exists(cur, tenant_id, room_id)
        task = _tenant_task(cur, tenant_id, task_id)
        if task is None:
            raise ValueError(f"no task {task_id!r} in this tenant")
        row = _insert_task_link(cur, tenant_id, room, task, origin, edition, suggestion_key)
        conn.commit()
        return _task(row)


def _tenant_task(cur: Any, tenant_id: str, task_id: str) -> dict[str, Any] | None:
    canonical = canonical_uuid(task_id)
    if canonical is None:
        return None
    cur.execute(
        "SELECT id, title FROM crm_tasks WHERE tenant_id = %s AND id = %s AND deleted_at IS NULL",
        (tenant_id, canonical),
    )
    row = cur.fetchone()
    return {"id": str(row["id"]), "title": row["title"]} if row else None


def _insert_task_link(
    cur: Any,
    tenant_id: str,
    room_id: str,
    task: dict[str, Any],
    origin: str,
    edition_id: str | None,
    suggestion_key: str | None,
) -> Mapping[str, Any]:
    cur.execute(
        """INSERT INTO room_tasks (tenant_id, room_id, task_id, edition_id, origin, suggestion_key)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (tenant_id, room_id, task_id) DO NOTHING RETURNING *""",
        (tenant_id, room_id, task["id"], edition_id, origin, suggestion_key),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT * FROM room_tasks WHERE tenant_id = %s AND room_id = %s AND task_id = %s",
            (tenant_id, room_id, task["id"]),
        )
        row = cur.fetchone()
    return dict(row)


def _decide_task(
    tenant_id: str, room_id: str, task_id: str, user_id: str, column: str
) -> RoomTask | None:
    room, task, user = canonical_uuid(room_id), canonical_uuid(task_id), canonical_uuid(user_id)
    if room is None or task is None or user is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            # ``column`` is one of two constants below, never caller input.
            f"UPDATE room_tasks SET {column} = %s, updated_at = now() "  # noqa: S608
            "WHERE tenant_id = %s AND room_id = %s AND task_id = %s "
            "AND accepted_by IS NULL AND dismissed_by IS NULL RETURNING *",
            (user, tenant_id, room, task),
        )
        row = cur.fetchone()
        conn.commit()
        return _task(row) if row else None


def accept_task(tenant_id: str, room_id: str, task_id: str, user_id: str) -> RoomTask | None:
    """Accept a linked task. None when there is no such link or it was already
    accepted or dismissed: a task is decided once."""
    return _decide_task(tenant_id, room_id, task_id, user_id, "accepted_by")


def dismiss_task(tenant_id: str, room_id: str, task_id: str, user_id: str) -> RoomTask | None:
    """Dismiss a linked task. None as for ``accept_task``."""
    return _decide_task(tenant_id, room_id, task_id, user_id, "dismissed_by")


def list_room_tasks(tenant_id: str, room_id: str) -> list[RoomTask]:
    room = canonical_uuid(room_id)
    if room is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM room_tasks WHERE tenant_id = %s AND room_id = %s "
            "ORDER BY created_at, task_id",
            (tenant_id, room),
        )
        return [_task(r) for r in cur.fetchall()]


def get_or_create_task_room(
    tenant_id: str, task_id: str, *, agent_id: str, created_by: str
) -> Room | None:
    """The room of a CRM task, made on first use (kind ``task``, titled after
    the task, the task linked as its ``subject``). Racing callers get the same
    room. None when the task is not in the tenant."""
    made = ensure_task_room(tenant_id, task_id, agent_id=agent_id, created_by=created_by)
    return made[0] if made else None


def ensure_task_room(
    tenant_id: str, task_id: str, *, agent_id: str, created_by: str
) -> tuple[Room, bool] | None:
    """``get_or_create_task_room`` that also says whether this call made it
    (``created``): only the caller that made the room seeds it. None when the
    task is not in the tenant."""
    creator = _required_uuid(created_by, "created_by")
    room_session_key(agent_id, str(uuid.uuid4()))  # refuses an agent id that would not parse
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        task = _tenant_task(cur, tenant_id, task_id)
        if task is None:
            return None
        room = _insert_room(
            cur,
            tenant_id,
            kind="task",
            title=(task["title"] or "").strip() or "Task",
            agent_id=agent_id,
            created_by=creator,
            instructions="",
            settings={},
            task_id=task["id"],
            run_as_user_id=None,
        )
        created = room is not None
        if room is None:
            # Another caller made it; ON CONFLICT waited for its commit.
            cur.execute(
                "SELECT * FROM rooms WHERE tenant_id = %s AND task_id = %s",
                (tenant_id, task["id"]),
            )
            room = _room(cur.fetchone())
        else:
            _insert_task_link(cur, tenant_id, room.id, task, "subject", None, None)
        conn.commit()
        return room, created


def get_task_room(tenant_id: str, task_id: str) -> Room | None:
    """The room opened for a CRM task, if there is one."""
    task = canonical_uuid(task_id)
    if task is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM rooms WHERE tenant_id = %s AND task_id = %s", (tenant_id, task))
        row = cur.fetchone()
        return _room(row) if row else None


def join_task_room(tenant_id: str, room_id: str, user_id: str) -> RoomMember | None:
    """Add ``user_id`` to a task room as assigned, unless they were removed
    from it. An existing membership is returned unchanged. None when the
    person was removed (only someone who can share the room re-adds them)."""
    room, user = canonical_uuid(room_id), canonical_uuid(user_id)
    if room is None or user is None:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """INSERT INTO room_members (tenant_id, room_id, user_id, assigned, added_by)
               SELECT %(t)s, %(r)s, %(u)s, TRUE, %(u)s
               WHERE NOT EXISTS (SELECT 1 FROM room_member_removals
                                 WHERE tenant_id = %(t)s AND room_id = %(r)s AND user_id = %(u)s)
               ON CONFLICT (tenant_id, room_id, user_id) DO NOTHING RETURNING *""",
            {"t": tenant_id, "r": room, "u": user},
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT * FROM room_members WHERE tenant_id = %s AND room_id = %s AND user_id = %s",
                (tenant_id, room, user),
            )
            row = cur.fetchone()
        conn.commit()
        return _member(row) if row else None


# ─── tasks as people see them ────────────────────────────────────────────

#: A CRM task's fields the rooms layer reads. Never written by this module
#: except ``assignee_id`` (accept) and ``resolved_at``/``resolution`` (dismiss).
_TASK_FIELDS = (
    "id, title, status, objective, assigned_to_agent, assignee_id, tags, next_action, resolved_at"
)


def get_crm_task(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    """A CRM task of the tenant (not deleted), or None."""
    task = canonical_uuid(task_id)
    if task is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT {_TASK_FIELDS} FROM crm_tasks "  # noqa: S608 - a module constant
            "WHERE tenant_id = %s AND id = %s AND deleted_at IS NULL",
            (tenant_id, task),
        )
        row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    out["assignee_id"] = _s(out["assignee_id"])
    return out


def task_history(tenant_id: str, task_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """The task's most recent transitions, oldest first."""
    task = canonical_uuid(task_id)
    if task is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT from_status, to_status, changed_by, reason, created_at FROM crm_task_history "
            "WHERE tenant_id = %s AND task_id = %s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, task, max(1, int(limit))),
        )
        return [dict(r) for r in reversed(cur.fetchall())]


_VIEW_SELECT = """
SELECT rt.task_id, rt.origin, rt.edition_id, rt.suggestion_key, rt.accepted_by,
       rt.dismissed_by, t.title, t.status, t.assignee_id,
       ua.display_name AS assignee_name, ub.display_name AS accepted_name
FROM room_tasks rt
JOIN crm_tasks t ON t.tenant_id = rt.tenant_id AND t.id = rt.task_id AND t.deleted_at IS NULL
LEFT JOIN user_accounts ua ON ua.tenant_id = rt.tenant_id AND ua.id = t.assignee_id
LEFT JOIN user_accounts ub ON ub.tenant_id = rt.tenant_id AND ub.id = rt.accepted_by
WHERE rt.tenant_id = %(t)s AND rt.room_id = %(r)s"""


def _view_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("task_id", "edition_id", "accepted_by", "dismissed_by", "assignee_id"):
        out[key] = _s(out[key])
    return out


def room_task_views(
    tenant_id: str, room_id: str, *, include_dismissed: bool = False
) -> list[dict[str, Any]]:
    """The room's linked tasks with their CRM title, status and human
    assignee and the display names of the assignee and the accepter."""
    room = canonical_uuid(room_id)
    if room is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            _VIEW_SELECT
            + (" " if include_dismissed else " AND rt.dismissed_by IS NULL ")
            + "ORDER BY rt.created_at, rt.task_id",
            {"t": tenant_id, "r": room},
        )
        return [_view_row(r) for r in cur.fetchall()]


def room_task_view(tenant_id: str, room_id: str, task_id: str) -> dict[str, Any] | None:
    room, task = canonical_uuid(room_id), canonical_uuid(task_id)
    if room is None or task is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            _VIEW_SELECT + " AND rt.task_id = %(k)s", {"t": tenant_id, "r": room, "k": task}
        )
        row = cur.fetchone()
        return _view_row(row) if row else None


def _locked_link(cur: Any, tenant_id: str, room: str, task: str) -> dict[str, Any] | None:
    """The link and its CRM task, both row-locked (link first, as every writer does)."""
    cur.execute(
        "SELECT rt.origin, rt.accepted_by, rt.dismissed_by FROM room_tasks rt "
        "WHERE rt.tenant_id = %s AND rt.room_id = %s AND rt.task_id = %s FOR UPDATE",
        (tenant_id, room, task),
    )
    link = cur.fetchone()
    if link is None:
        return None
    cur.execute(
        "SELECT assignee_id FROM crm_tasks WHERE tenant_id = %s AND id = %s "
        "AND deleted_at IS NULL FOR UPDATE",
        (tenant_id, task),
    )
    crm = cur.fetchone()
    if crm is None:
        return None
    return {
        "origin": link["origin"],
        "accepted_by": _s(link["accepted_by"]),
        "dismissed_by": _s(link["dismissed_by"]),
        "assignee_id": _s(crm["assignee_id"]),
    }


def take_task(tenant_id: str, room_id: str, task_id: str, user_id: str) -> tuple[str, str | None]:
    """Accept a linked task for ``user_id``: the link's ``accepted_by`` and the
    CRM task's human assignee (``crm_tasks.assignee_id``), in one transaction.

    ``(outcome, holder)``: ``accepted`` (also when ``user_id`` already had it),
    ``taken`` with the user who has it, ``dismissed``, or ``missing``."""
    room, task, user = canonical_uuid(room_id), canonical_uuid(task_id), canonical_uuid(user_id)
    if room is None or task is None or user is None:
        return "missing", None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        link = _locked_link(cur, tenant_id, room, task)
        if link is None:
            return "missing", None
        if link["dismissed_by"] is not None:
            return "dismissed", None
        holder = link["assignee_id"] or link["accepted_by"]
        if holder is not None and holder != user:
            return "taken", holder
        cur.execute(
            "UPDATE room_tasks SET accepted_by = %s, updated_at = now() "
            "WHERE tenant_id = %s AND room_id = %s AND task_id = %s AND accepted_by IS NULL",
            (user, tenant_id, room, task),
        )
        cur.execute(
            "UPDATE crm_tasks SET assignee_id = %s, updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND assignee_id IS NULL",
            (user, tenant_id, task),
        )
        conn.commit()
        return "accepted", user


def dismiss_suggestion(
    tenant_id: str, room_id: str, task_id: str, user_id: str
) -> tuple[str, str | None]:
    """Dismiss a suggested task: the link records who, and the CRM task is
    resolved as ``dismissed`` so it leaves open task lists.

    ``(outcome, holder)``: ``dismissed``, ``already_dismissed``, ``accepted``
    with who has it, ``not_suggestion`` or ``missing``."""
    room, task, user = canonical_uuid(room_id), canonical_uuid(task_id), canonical_uuid(user_id)
    if room is None or task is None or user is None:
        return "missing", None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        link = _locked_link(cur, tenant_id, room, task)
        if link is None:
            return "missing", None
        if link["origin"] != "suggestion":
            return "not_suggestion", None
        if link["dismissed_by"] is not None:
            return "already_dismissed", None
        holder = link["accepted_by"] or link["assignee_id"]
        if holder is not None:
            return "accepted", holder
        cur.execute(
            "UPDATE room_tasks SET dismissed_by = %s, updated_at = now() "
            "WHERE tenant_id = %s AND room_id = %s AND task_id = %s",
            (user, tenant_id, room, task),
        )
        cur.execute(
            "UPDATE crm_tasks SET resolved_at = now(), resolution = 'dismissed', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND resolved_at IS NULL",
            (tenant_id, task),
        )
        conn.commit()
        return "dismissed", None


def create_suggested_tasks(
    tenant_id: str,
    room_id: str,
    edition_id: str,
    suggestions: list[dict[str, Any]],
    *,
    created_by_agent: str | None = None,
) -> list[str]:
    """One CRM task per suggestion key the room has never had, linked with
    ``origin='suggestion'``, the edition and the key. Returns the new task ids.

    The tasks wait for a person: ``requires_human``, no agent assignee, no
    tags (never ``thread``, so the planner leaves them alone). The room row
    lock serialises concurrent publishes; the unique key index backs it."""
    edition = _required_uuid(edition_id, "edition_id")
    created: list[str] = []
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        room = _locked_room(cur, tenant_id, room_id)
        cur.execute(
            "SELECT suggestion_key FROM room_tasks WHERE tenant_id = %s AND room_id = %s "
            "AND suggestion_key IS NOT NULL",
            (tenant_id, room.id),
        )
        seen = {r["suggestion_key"] for r in cur.fetchall()}
        for item in suggestions:
            key = item.get("key") if isinstance(item, Mapping) else None
            if not isinstance(key, str) or not key.strip() or key in seen:
                continue
            seen.add(key)
            task_id = str(uuid.uuid4())
            title = str(item.get("title") or key).strip() or key
            cur.execute(
                """INSERT INTO crm_tasks (id, tenant_id, title, body, status, priority, tags,
                                         requires_human, created_by_agent)
                   VALUES (%s, %s, %s, %s, 'TODO', 'normal', '{}', TRUE, %s)""",
                (task_id, tenant_id, title, item.get("detail") or None, created_by_agent),
            )
            cur.execute(
                """INSERT INTO crm_task_history (id, task_id, from_status, to_status, changed_by,
                                                reason, tenant_id)
                   VALUES (%s, %s, NULL, 'TODO', %s, 'Suggested in a workspace edition', %s)""",
                (str(uuid.uuid4()), task_id, created_by_agent or "system", tenant_id),
            )
            cur.execute(
                """INSERT INTO room_tasks (tenant_id, room_id, task_id, edition_id, origin,
                                          suggestion_key)
                   VALUES (%s, %s, %s, %s, 'suggestion', %s)""",
                (tenant_id, room.id, task_id, edition, key),
            )
            created.append(task_id)
        conn.commit()
    return created


def rooms_linked_to_task(tenant_id: str, task_id: str) -> list[str]:
    """Every room the task is linked to (as subject, suggestion or link)."""
    task = canonical_uuid(task_id)
    if task is None:
        return []
    with get_connection(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT room_id FROM room_tasks WHERE tenant_id = %s AND task_id = %s ORDER BY room_id",
            (tenant_id, task),
        )
        return [str(r[0]) for r in cur.fetchall()]
