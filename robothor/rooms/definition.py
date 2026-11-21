"""A workspace's definition: create, change, share, restore, duplicate, read.

The one place the rules for a room's definition live. The main agent's
``room_*`` tools (``robothor/engine/tools/handlers/rooms.py``) and the
bridge's ``/api/rooms`` routes are thin wrappers over these functions, so a
room created in conversation and one created in the dashboard are checked the
same way: access through ``robothor.rooms.access``, instructions through the
injection scanner, the schedule through ``cron_parse`` and the room minimum,
every definition change as a numbered revision, and the room's one schedule
row in ``user_cronjobs`` kept in step with the room.

Sync, like the data layer; async callers use ``asyncio.to_thread``. Errors are
the rooms layer's (``RoomNotFound``, ``RoomForbidden``, ``RevisionConflict``)
plus ``ValueError`` for a request that cannot be applied, of which
``UnsafeInstructions`` and ``InvalidSchedule`` are the two named kinds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.rooms import access, dal
from robothor.rooms.access import Actor
from robothor.rooms.errors import RevisionConflict, RoomForbidden, RoomNotFound
from robothor.rooms.keys import canonical_uuid

if TYPE_CHECKING:
    from robothor.rooms.models import Room, RoomMember, RoomSummary

__all__ = [
    "CREATABLE_KINDS",
    "DEFAULT_AGENT",
    "ROOM_MIN_INTERVAL_SECONDS",
    "InvalidSchedule",
    "TenantUser",
    "UnsafeInstructions",
    "agent_exists",
    "apply_definition_change",
    "archive_room",
    "create_room_with_schedule",
    "duplicate_room",
    "list_rooms",
    "read_room",
    "reassign_room",
    "resolve_user",
    "restore_room_revision",
    "room_schedule",
    "run_as_actor",
    "share_room",
    "sync_room_schedule",
    "unshare_room",
]

#: The shortest gap between two scheduled runs of one room: 15 minutes. A
#: report that refreshes more often than this is a dashboard, and each run is
#: a full agent turn with its own cost.
ROOM_MIN_INTERVAL_SECONDS = 900

#: Task rooms come from a CRM task (``dal.get_or_create_task_room``), never here.
CREATABLE_KINDS = frozenset({"report", "agent"})

#: The agent a room talks to when none is named.
DEFAULT_AGENT = "main"

#: What ``apply_definition_change`` accepts. ``agent_id`` is not one: it is
#: part of the session key, so a room keeps its agent; duplicate to change it.
CHANGE_KEYS = frozenset(
    {
        "title",
        "instructions",
        "schedule_text",
        "tz",
        "default_grain",
        "paused",
        "notify_default",
        "max_cost_usd",
    }
)
_SETTING_CHANGES = ("tz", "default_grain", "notify_default", "max_cost_usd")
_SCHEDULE_CHANGES = frozenset({"schedule_text", "tz", "paused"})
_STAFF_ROLES = frozenset({"owner", "admin", "member", "user"})
_ADMIN_ROLES = frozenset({"owner", "admin"})
#: How many upcoming fire times of a cron schedule are checked for the minimum.
_CRON_SAMPLE = 48


class UnsafeInstructions(ValueError):  # noqa: N818 - name fixed by the lane contract
    """The instructions (or title) trip the injection scanner."""


class InvalidSchedule(ValueError):  # noqa: N818 - name fixed by the lane contract
    """The schedule does not parse, does not repeat, or runs too often."""


@dataclass(frozen=True)
class TenantUser:
    """An active account of the tenant."""

    user_id: str
    role: str
    display_name: str


# ─── checks ──────────────────────────────────────────────────────────────


def agent_exists(agent_id: str) -> bool:
    """Whether ``agent_id`` names an agent this instance can load."""
    from robothor.engine.config import EngineConfig, load_agent_config

    try:
        return load_agent_config(agent_id, EngineConfig.from_env().manifest_dir) is not None
    except Exception:
        return False


def _scan(*texts: str | None) -> None:
    from robothor.engine.cron_safety import scan_assembled_cron_prompt

    for text in texts:
        finding = scan_assembled_cron_prompt(text or "")
        if finding is not None:
            raise UnsafeInstructions(
                f"the instructions were refused by the injection scanner ({finding})"
            )


def _too_fast() -> InvalidSchedule:
    return InvalidSchedule(
        f"a workspace runs at most every {ROOM_MIN_INTERVAL_SECONDS // 60} minutes"
    )


def _parse_schedule(text: str, tz: str | None) -> dict[str, Any]:
    """The ``parse_schedule`` payload for a room, or ``InvalidSchedule``."""
    from robothor.engine.cron_parse import parse_schedule
    from robothor.engine.crontab import cron_trigger

    try:
        schedule = parse_schedule(text)
    except ValueError as exc:
        raise InvalidSchedule(f"invalid schedule {text!r}: {exc}") from exc
    if schedule["kind"] == "once":
        raise InvalidSchedule(
            f"{text!r} runs once; a workspace schedule repeats (for one run now, use run now)"
        )
    if schedule["kind"] == "interval":
        if int(schedule["every_seconds"]) < ROOM_MIN_INTERVAL_SECONDS:
            raise _too_fast()
        return schedule
    try:
        trigger = cron_trigger(str(schedule["expression"]), timezone=tz or "UTC")
    except ValueError as exc:
        raise InvalidSchedule(f"invalid schedule {text!r}: {exc}") from exc
    fires: list[datetime] = []
    previous: datetime | None = None
    moment = datetime.now(UTC)
    for _ in range(_CRON_SAMPLE):
        nxt = trigger.get_next_fire_time(previous, moment)
        if nxt is None:
            break
        fires.append(nxt)
        previous, moment = nxt, nxt
    gaps = [(b - a).total_seconds() for a, b in zip(fires, fires[1:], strict=False)]
    if not fires or (gaps and min(gaps) < ROOM_MIN_INTERVAL_SECONDS):
        raise _too_fast()
    return {**schedule, "tz": tz or "UTC"}


def _check_tz(tz: Any) -> None:
    if tz is None:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {tz!r}") from exc


def _check_creator(actor: Actor) -> None:
    if actor.is_service or actor.role not in _STAFF_ROLES:
        role = "a service identity" if actor.is_service else f"the {actor.role or 'unknown'} role"
        raise RoomForbidden(f"{role} cannot create workspaces; a viewer may only list and read")


def _check_setting_values(changes: Mapping[str, Any]) -> None:
    cost = changes.get("max_cost_usd")
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0
    ):
        raise ValueError("max_cost_usd must be a number of dollars, zero or more")
    notify = changes.get("notify_default")
    if notify is not None and not isinstance(notify, bool | str | dict):
        raise ValueError("notify_default must be true/false, a name or an object")
    if "paused" in changes and not isinstance(changes["paused"], bool):
        raise ValueError("paused must be true or false")
    _check_tz(changes.get("tz"))


# ─── people ──────────────────────────────────────────────────────────────


def resolve_user(tenant_id: str, user: str) -> TenantUser | None:
    """An active account of ``tenant_id`` by id or email, else None."""
    from robothor.auth.accounts import canonical_email

    if not isinstance(user, str) or not user.strip():
        return None
    user_id = canonical_uuid(user.strip())
    column, value = ("id", user_id) if user_id else ("email", canonical_email(user.strip()))
    if column == "email" and "@" not in value:
        return None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            # ``column`` is one of two constants above, never caller input.
            "SELECT id, role, display_name FROM user_accounts "  # noqa: S608
            f"WHERE tenant_id = %s AND status = 'active' AND {column} = %s",
            (tenant_id, value),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return TenantUser(str(row["id"]), str(row["role"]), str(row["display_name"] or ""))


def _require_user(tenant_id: str, user: str) -> TenantUser:
    found = resolve_user(tenant_id, user)
    if found is None:
        raise ValueError(f"no active user {user!r} in this company")
    return found


def _display_names(tenant_id: str, user_ids: Iterable[str | None]) -> dict[str, str]:
    ids = sorted({c for c in (canonical_uuid(u) for u in user_ids) if c})
    if not ids:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id::text AS id, display_name FROM user_accounts "
            "WHERE tenant_id = %s AND id = ANY(%s::uuid[])",
            (tenant_id, ids),
        )
        return {r["id"]: r["display_name"] or "" for r in cur.fetchall()}


def run_as_actor(tenant_id: str, room_id: str) -> Actor | None:
    """The room's run-as user as an actor, with their CURRENT role. None when
    the room is not in the tenant or the user is no longer an active account."""
    room = dal.get_room(tenant_id, room_id)
    if room is None or not room.run_as_user_id:
        return None
    user = resolve_user(tenant_id, room.run_as_user_id)
    if user is None:
        return None
    return Actor(user_id=user.user_id, role=user.role, tenant_id=tenant_id)


# ─── the schedule row ────────────────────────────────────────────────────


def _sync_schedule(room: Room) -> dict[str, Any] | None:
    from robothor.engine.user_cron import sync_room_cronjob

    text = room.settings.get("schedule_text")
    schedule = _parse_schedule(text, room.settings.get("tz")) if text else None
    return sync_room_cronjob(
        tenant_id=room.tenant_id,
        room_id=room.id,
        agent_id=room.agent_id,
        prompt=room.instructions,
        schedule=schedule,
        run_as_user_id=room.run_as_user_id,
        enabled=not room.paused and not room.archived,
    )


def sync_room_schedule(room: Room) -> dict[str, Any] | None:
    """Make the room's schedule row match ``room`` as it now stands (paused,
    archived or running). For a caller outside the definition rules, such as
    a run that pauses its room."""
    return _sync_schedule(room)


def room_schedule(tenant_id: str, room_id: str) -> dict[str, Any] | None:
    """``{job_id, enabled, next_run_at, run_as_user_id}`` of the room's schedule row."""
    from robothor.engine.user_cron import get_room_cronjob

    row = get_room_cronjob(tenant_id, room_id)
    if row is None:
        return None
    return {
        "job_id": row["job_id"],
        "enabled": bool(row["enabled"]),
        "next_run_at": row["next_run_at"].isoformat() if row["next_run_at"] else None,
        "run_as_user_id": row["run_as_user_id"],
    }


# ─── create and change ───────────────────────────────────────────────────


def create_room_with_schedule(
    actor: Actor,
    *,
    kind: str,
    title: str,
    instructions: str,
    agent_id: str | None = None,
    schedule_text: str | None = None,
    tz: str | None = None,
    default_grain: str | None = None,
    share_with: Iterable[str] = (),
) -> Room:
    """A new room: the caller assigned and run-as, revision 1, ``share_with``
    added as shared members, and, when scheduled, its schedule row.

    Everything is checked before anything is written: the caller's role, the
    kind, the agent, the instructions, the time zone, the schedule and every
    person in ``share_with``.
    """
    _check_creator(actor)
    if kind not in CREATABLE_KINDS:
        raise ValueError(f"kind must be one of {sorted(CREATABLE_KINDS)}")
    agent = DEFAULT_AGENT if agent_id is None else agent_id
    if not isinstance(agent, str) or not agent or ":" in agent or not agent_exists(agent):
        raise ValueError(f"no agent {agent!r} is available for a workspace")
    _scan(title, instructions)
    _check_tz(tz)
    text = (schedule_text or "").strip() or None
    if text:
        _parse_schedule(text, tz)
    members = [_require_user(actor.tenant_id, u) for u in share_with or ()]
    settings = {
        key: value
        for key, value in (("tz", tz), ("default_grain", default_grain), ("schedule_text", text))
        if value is not None
    }
    room = dal.create_room(
        actor.tenant_id,
        kind=kind,
        title=title,
        agent_id=agent,
        created_by=actor.user_id,
        instructions=instructions or "",
        settings=settings,
    )
    for member in members:
        if member.user_id != actor.user_id:
            dal.add_member(actor.tenant_id, room.id, member.user_id, added_by=actor.user_id)
    if text:
        _sync_schedule(room)
    return room


def _definition_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: changes[k] for k in ("title", "instructions") if k in changes}
    settings = {k: changes[k] for k in _SETTING_CHANGES if k in changes}
    if settings:
        out["settings"] = settings
    if "schedule_text" in changes:
        out["schedule"] = (changes["schedule_text"] or "").strip() or None
    return out


def apply_definition_change(
    actor: Actor, room_id: str, expected_rev: int, changes: Mapping[str, Any]
) -> Room:
    """Apply ``changes`` to a room the caller is assigned to (or administers),
    when it is still at ``expected_rev``.

    A change to the definition writes the next revision; pausing alone does
    not, but still checks the revision. Changed instructions are scanned again
    and a changed schedule parsed again, before anything is written. The
    schedule row follows a change of schedule, time zone or pause; a resumed
    room's next run is computed from now. A shared caller gets ``RoomForbidden``.
    """
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("nothing to change")
    if "agent_id" in changes:
        raise ValueError("a workspace keeps its agent; duplicate it to use another agent")
    unknown = sorted(set(changes) - CHANGE_KEYS)
    if unknown:
        raise ValueError(f"cannot change {', '.join(unknown)}")
    if isinstance(expected_rev, bool) or not isinstance(expected_rev, int):
        raise ValueError("expected_rev must be the revision number you read")
    room = access.require(actor, room_id, "edit_definition")
    _check_setting_values(changes)
    _scan(changes.get("title"), changes.get("instructions"))
    text = (
        changes["schedule_text"]
        if "schedule_text" in changes
        else room.settings.get("schedule_text")
    )
    tz = changes["tz"] if "tz" in changes else room.settings.get("tz")
    if text and _SCHEDULE_CHANGES & set(changes):
        _parse_schedule(str(text), tz)
    definition = _definition_changes(changes)
    if definition:
        saved = dal.update_definition(
            actor.tenant_id, room.id, definition, actor.user_id, expected_rev
        )
    elif room.rev != expected_rev:
        raise RevisionConflict(room.rev)
    else:
        saved = room
    if "paused" in changes and changes["paused"] != saved.paused:
        saved = dal.set_paused(actor.tenant_id, room.id, changes["paused"])
    if _SCHEDULE_CHANGES & set(changes):
        _sync_schedule(saved)
    return saved


def restore_room_revision(actor: Actor, room_id: str, rev: int) -> Room:
    """Write a new revision equal to revision ``rev``, re-checked as a change."""
    room = access.require(actor, room_id, "restore_revision")
    old = next((r for r in dal.list_revisions(actor.tenant_id, room.id) if r.rev == rev), None)
    if old is None:
        raise ValueError(f"the workspace has no revision {rev}")
    settings = dict(old.definition.get("settings") or {})
    _scan(old.definition.get("title"), old.definition.get("instructions"))
    if settings.get("schedule_text"):
        _parse_schedule(str(settings["schedule_text"]), settings.get("tz"))
    saved = dal.restore_revision(actor.tenant_id, room.id, rev, actor.user_id)
    _sync_schedule(saved)
    return saved


def duplicate_room(actor: Actor, room_id: str) -> Room:
    """The caller's own copy: same definition, the caller assigned and run-as,
    no editions, conversation or tasks. A scheduled copy starts paused, so it
    runs only once its new owner resumes it."""
    room = access.require(actor, room_id, "duplicate")
    _check_creator(actor)
    copy = dal.duplicate_room(actor.tenant_id, room.id, actor.user_id)
    if copy.settings.get("schedule_text"):
        copy = dal.set_paused(actor.tenant_id, copy.id, True)
    return copy


def archive_room(actor: Actor, room_id: str) -> Room:
    """Archive a room; its schedule stops."""
    room = access.require(actor, room_id, "archive")
    archived = dal.archive_room(actor.tenant_id, room.id)
    if archived is None:
        raise RoomNotFound(room_id)
    _sync_schedule(archived)
    return archived


# ─── people in a room ────────────────────────────────────────────────────


def share_room(actor: Actor, room_id: str, user: str, *, assigned: bool = False) -> RoomMember:
    """Add a colleague of the same company, shared or (``assigned``) able to manage it."""
    room = access.require(actor, room_id, "share")
    target = _require_user(actor.tenant_id, user)
    if assigned and target.role not in _STAFF_ROLES:
        raise ValueError(f"{target.display_name or user} is a viewer and cannot be assigned")
    return dal.add_member(
        actor.tenant_id, room.id, target.user_id, added_by=actor.user_id, assigned=bool(assigned)
    )


def unshare_room(actor: Actor, room_id: str, user: str) -> str:
    """Remove a member; returns their user id. Never the run-as user, nor the
    last assigned member: reassign first."""
    room = access.require(actor, room_id, "share")
    user_id = canonical_uuid(user)
    if user_id is None:
        found = resolve_user(actor.tenant_id, user)
        user_id = found.user_id if found else None
    members = dal.list_members(actor.tenant_id, room.id)
    member = next((m for m in members if m.user_id == user_id), None)
    if member is None:
        raise ValueError(f"{user!r} is not a member of this workspace")
    if member.user_id == room.run_as_user_id:
        raise ValueError("they are who this workspace runs as; reassign it before removing them")
    if member.assigned and sum(m.assigned for m in members) <= 1:
        raise ValueError("they are the last assigned member; assign someone else first")
    dal.remove_member(actor.tenant_id, room.id, member.user_id)
    return member.user_id


def reassign_room(actor: Actor, room_id: str, user: str) -> Room:
    """An admin hands a room to a colleague: assigned, and the one it runs as."""
    room = access.require(actor, room_id, "reassign")
    target = _require_user(actor.tenant_id, user)
    if target.role not in _STAFF_ROLES:
        raise ValueError(f"{target.display_name or user} is a viewer and cannot run a workspace")
    dal.add_member(actor.tenant_id, room.id, target.user_id, added_by=actor.user_id, assigned=True)
    moved = dal.set_run_as(actor.tenant_id, room.id, target.user_id)
    if moved.settings.get("schedule_text"):
        _sync_schedule(moved)
    return moved


# ─── read and list ───────────────────────────────────────────────────────


def list_rooms(actor: Actor, *, scope: str = "mine") -> list[RoomSummary]:
    """``mine``: the rooms the caller is a member of. ``all``: every room of the
    company, for an owner or admin."""
    if scope == "mine":
        if actor.is_service:
            return []
        return dal.list_rooms_for_user(actor.tenant_id, actor.user_id)
    if scope == "all":
        if actor.is_service or actor.role not in _ADMIN_ROLES:
            raise RoomForbidden("only an owner or admin may list every workspace")
        return dal.list_all_rooms(actor.tenant_id)
    raise ValueError("scope must be 'mine' or 'all'")


def _revision_history(tenant_id: str, room_id: str, limit: int = 10) -> list[dict[str, Any]]:
    revisions = dal.list_revisions(tenant_id, room_id)
    names = _display_names(tenant_id, (r.changed_by for r in revisions[:limit]))
    out = []
    for i, rev in enumerate(revisions[:limit]):
        before = revisions[i + 1].definition if i + 1 < len(revisions) else {}
        keys = sorted(set(rev.definition) | set(before))
        changed = [k for k in keys if rev.definition.get(k) != before.get(k)]
        if "settings" in changed and "schedule" in changed:
            changed.remove("settings")
        out.append(
            {
                "rev": rev.rev,
                "changed_by": rev.changed_by,
                "changed_by_name": names.get(rev.changed_by or "", ""),
                "changed_at": rev.changed_at,
                "changed": changed,
            }
        )
    return out


def read_room(
    actor: Actor, room_id: str, *, editions: int = 3, messages: int = 10
) -> dict[str, Any]:
    """Everything the caller may see of a room: the room, their access, its
    members, who it runs as, its schedule, the latest editions (previews only
    their own), the last messages with authors, linked tasks and revisions."""
    room = access.require(actor, room_id, "read")
    member = None
    if actor.role not in _ADMIN_ROLES:
        member = dal.get_member(actor.tenant_id, room.id, actor.user_id)
    members = dal.list_members(actor.tenant_id, room.id)
    recent = dal.recent_messages(actor.tenant_id, room.id, limit=max(1, min(int(messages), 50)))
    names = _display_names(
        actor.tenant_id,
        [m.user_id for m in members]
        + [room.run_as_user_id]
        + [m["author_user_id"] for m in recent],
    )
    for message in recent:
        message["author_name"] = names.get(message["author_user_id"] or "", "")
    return {
        "room": room,
        "access": access.room_access(actor, room, member),
        "link": room.link,
        "members": [
            {"user_id": m.user_id, "display_name": names.get(m.user_id, ""), "assigned": m.assigned}
            for m in members
        ],
        "runs_as": (
            {"user_id": room.run_as_user_id, "display_name": names.get(room.run_as_user_id, "")}
            if room.run_as_user_id
            else None
        ),
        "schedule": room_schedule(actor.tenant_id, room.id),
        "editions": dal.list_editions(
            actor.tenant_id,
            room.id,
            viewer_id=actor.user_id,
            limit=max(1, min(int(editions), 20)),
        ),
        "messages": recent,
        "tasks": dal.list_room_tasks(actor.tenant_id, room.id),
        "revisions": _revision_history(actor.tenant_id, room.id),
    }
