"""The rooms data layer against real Postgres.

Stories: a definition nobody can overwrite by accident (numbered revisions,
conflicts, restore); per-thread access rows; per-user unread counts; editions
with one building row per period and previews nobody else sees; linked tasks
decided once; one room per CRM task however many callers race for it; and the
company boundary — another tenant's ids return nothing.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from datetime import date
from typing import Any

import psycopg2
import pytest

from robothor.rooms import dal
from robothor.rooms.access import room_session_key
from robothor.rooms.errors import InvalidBlocks, RevisionConflict, RoomNotFound
from robothor.rooms.tests.conftest import committed_connection

pytestmark = pytest.mark.integration

BLOCKS = [{"component": "render_markdown", "props": {"content": "All good."}}]


def _room(db: Any, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "kind": "report",
        "title": "Weekly numbers",
        "agent_id": "main",
        "created_by": db.alice,
        "instructions": "Summarise the week.",
        "settings": {"tz": "UTC", "default_grain": "week"},
    }
    fields.update(overrides)
    tenant = fields.pop("tenant_id", db.tenant)
    return dal.create_room(tenant, **fields)


def _task(db: Any, tenant: str | None = None, title: str = "Call Bob back") -> str:
    db.cur.execute(
        "INSERT INTO crm_tasks (tenant_id, title) VALUES (%s, %s) RETURNING id",
        (tenant or db.tenant, title),
    )
    return str(db.cur.fetchone()[0])


def _messages(db: Any, room: Any, n: int) -> list[int]:
    db.cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES (%s, %s, 'webchat') "
        "ON CONFLICT (tenant_id, session_key) DO UPDATE SET channel = EXCLUDED.channel "
        "RETURNING id",
        (room.tenant_id, room.session_key),
    )
    session_id = db.cur.fetchone()[0]
    ids = []
    for i in range(n):
        db.cur.execute(
            "INSERT INTO chat_messages (session_id, message) "
            "VALUES (%s, jsonb_build_object('role', 'user', 'content', %s::text)) RETURNING id",
            (session_id, f"message {i}"),
        )
        ids.append(int(db.cur.fetchone()[0]))
    return ids


def _race(n: int, fn: Any) -> list[Any]:
    """Run ``fn`` in ``n`` threads released together; results or exceptions."""
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - the caller inspects it
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


# ─── create, get ─────────────────────────────────────────────────────────


def test_create_writes_revision_one_and_the_creator_as_assigned(rooms_db) -> None:
    room = _room(rooms_db)

    assert room.rev == 1
    assert room.tenant_id == rooms_db.tenant
    assert room.session_key == room_session_key("main", room.id)
    assert room.run_as_user_id == rooms_db.alice
    assert room.paused is False and room.archived_at is None
    revisions = dal.list_revisions(rooms_db.tenant, room.id)
    assert [r.rev for r in revisions] == [1]
    assert revisions[0].changed_by == rooms_db.alice
    assert revisions[0].definition == {
        "title": "Weekly numbers",
        "instructions": "Summarise the week.",
        "agent_id": "main",
        "settings": {"tz": "UTC", "default_grain": "week"},
        "schedule": None,
    }
    members = dal.list_members(rooms_db.tenant, room.id)
    assert [(m.user_id, m.assigned) for m in members] == [(rooms_db.alice, True)]
    assert dal.get_room(rooms_db.tenant, room.id) == room


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "dashboard"},
        {"title": "   "},
        {"agent_id": "ma:in"},
        {"settings": {"colour": "blue"}},
        {"settings": {"default_grain": "fortnight"}},
    ],
)
def test_create_refuses_a_bad_definition(rooms_db, overrides) -> None:
    with pytest.raises(ValueError):
        _room(rooms_db, **overrides)


def test_get_room_of_a_missing_or_malformed_id_is_none(rooms_db) -> None:
    assert dal.get_room(rooms_db.tenant, str(uuid.uuid4())) is None
    assert dal.get_room(rooms_db.tenant, "not-a-uuid") is None


# ─── revisions ───────────────────────────────────────────────────────────


def test_update_writes_the_next_revision_and_bumps_rev(rooms_db) -> None:
    room = _room(rooms_db)

    updated = dal.update_definition(
        rooms_db.tenant, room.id, {"title": "Monthly numbers"}, rooms_db.bob, expected_rev=1
    )

    assert updated.rev == 2 and updated.title == "Monthly numbers"
    revisions = dal.list_revisions(rooms_db.tenant, room.id)
    assert [r.rev for r in revisions] == [2, 1]
    assert revisions[0].changed_by == rooms_db.bob
    assert revisions[0].definition["title"] == "Monthly numbers"
    assert dal.get_room(rooms_db.tenant, room.id).rev == 2


def test_a_stale_revision_is_refused_and_nothing_changes(rooms_db) -> None:
    room = _room(rooms_db)
    dal.update_definition(rooms_db.tenant, room.id, {"title": "B"}, rooms_db.bob, expected_rev=1)

    with pytest.raises(RevisionConflict) as caught:
        dal.update_definition(
            rooms_db.tenant, room.id, {"title": "C"}, rooms_db.alice, expected_rev=1
        )

    assert caught.value.current_rev == 2
    assert dal.get_room(rooms_db.tenant, room.id).title == "B"
    assert len(dal.list_revisions(rooms_db.tenant, room.id)) == 2


def test_two_concurrent_edits_of_one_revision_one_wins(rooms_db) -> None:
    room = _room(rooms_db)

    results = _race(
        4,
        lambda: dal.update_definition(
            rooms_db.tenant, room.id, {"title": uuid.uuid4().hex}, rooms_db.bob, expected_rev=1
        ),
    )

    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, RevisionConflict) for r in results) == 3
    assert [r.rev for r in dal.list_revisions(rooms_db.tenant, room.id)] == [2, 1]


def test_settings_merge_and_a_none_removes_a_key(rooms_db) -> None:
    room = _room(rooms_db)

    updated = dal.update_definition(
        rooms_db.tenant,
        room.id,
        {"settings": {"tz": "Europe/London", "default_grain": None, "max_cost_usd": 2}},
        rooms_db.alice,
        expected_rev=1,
    )

    assert updated.settings == {"tz": "Europe/London", "max_cost_usd": 2}


def test_a_schedule_change_lands_in_settings_and_the_definition(rooms_db) -> None:
    room = _room(rooms_db)

    updated = dal.update_definition(
        rooms_db.tenant, room.id, {"schedule": "every monday at 9"}, rooms_db.alice, expected_rev=1
    )

    assert updated.settings["schedule_text"] == "every monday at 9"
    assert dal.list_revisions(rooms_db.tenant, room.id)[0].definition["schedule"] == (
        "every monday at 9"
    )


@pytest.mark.parametrize(
    "changes",
    [{}, {"colour": "blue"}, {"agent_id": "helper"}, {"title": ""}, {"settings": {"x": 1}}],
)
def test_update_refuses_a_change_it_cannot_make(rooms_db, changes) -> None:
    room = _room(rooms_db)
    with pytest.raises(ValueError):
        dal.update_definition(rooms_db.tenant, room.id, changes, rooms_db.alice, expected_rev=1)
    assert dal.get_room(rooms_db.tenant, room.id).rev == 1


def test_restore_writes_a_new_revision_with_the_old_definition(rooms_db) -> None:
    room = _room(rooms_db)
    dal.update_definition(
        rooms_db.tenant,
        room.id,
        {"title": "Broken", "instructions": "Oops"},
        rooms_db.bob,
        expected_rev=1,
    )

    restored = dal.restore_revision(rooms_db.tenant, room.id, 1, rooms_db.alice)

    assert restored.rev == 3
    assert (restored.title, restored.instructions) == ("Weekly numbers", "Summarise the week.")
    revisions = dal.list_revisions(rooms_db.tenant, room.id)
    assert [r.rev for r in revisions] == [3, 2, 1]
    assert revisions[0].definition == revisions[2].definition
    assert revisions[0].changed_by == rooms_db.alice


def test_restore_checks_the_expected_revision_when_given(rooms_db) -> None:
    room = _room(rooms_db)
    dal.update_definition(rooms_db.tenant, room.id, {"title": "B"}, rooms_db.bob, expected_rev=1)
    with pytest.raises(RevisionConflict):
        dal.restore_revision(rooms_db.tenant, room.id, 1, rooms_db.alice, expected_rev=1)


def test_restoring_a_revision_that_does_not_exist_is_refused(rooms_db) -> None:
    room = _room(rooms_db)
    with pytest.raises(ValueError):
        dal.restore_revision(rooms_db.tenant, room.id, 9, rooms_db.alice)


# ─── duplicate, archive ──────────────────────────────────────────────────


def test_duplicate_copies_the_definition_only(rooms_db) -> None:
    room = _room(rooms_db)
    dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    edition, _ = dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)
    dal.link_task(rooms_db.tenant, room.id, _task(rooms_db))
    dal.update_definition(rooms_db.tenant, room.id, {"title": "V2"}, rooms_db.alice, 1)

    copy = dal.duplicate_room(rooms_db.tenant, room.id, rooms_db.bob)

    assert copy.id != room.id and copy.session_key != room.session_key
    assert copy.title == "V2 (copy)"
    assert (copy.kind, copy.agent_id, copy.instructions, copy.settings) == (
        "report",
        "main",
        "Summarise the week.",
        {"tz": "UTC", "default_grain": "week"},
    )
    assert copy.rev == 1 and copy.created_by == rooms_db.bob
    members = dal.list_members(rooms_db.tenant, copy.id)
    assert [(m.user_id, m.assigned) for m in members] == [(rooms_db.bob, True)]
    assert dal.list_editions(rooms_db.tenant, copy.id, viewer_id=None) == []
    assert dal.list_room_tasks(rooms_db.tenant, copy.id) == []
    assert edition.room_id == room.id


def test_duplicate_takes_a_title(rooms_db) -> None:
    room = _room(rooms_db)
    assert dal.duplicate_room(rooms_db.tenant, room.id, rooms_db.bob, title="Mine").title == "Mine"


def test_a_duplicated_task_room_is_an_agent_room_with_no_task(rooms_db) -> None:
    room = dal.get_or_create_task_room(
        rooms_db.tenant, _task(rooms_db), agent_id="main", created_by=rooms_db.alice
    )
    copy = dal.duplicate_room(rooms_db.tenant, room.id, rooms_db.alice)
    assert (copy.kind, copy.task_id) == ("agent", None)


def test_archive_and_unarchive(rooms_db) -> None:
    room = _room(rooms_db)
    assert dal.archive_room(rooms_db.tenant, room.id).archived_at is not None
    assert dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice) == []
    listed = dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice, include_archived=True)
    assert [(s.id, s.archived) for s in listed] == [(room.id, True)]
    assert dal.unarchive_room(rooms_db.tenant, room.id).archived_at is None
    assert [s.id for s in dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice)] == [room.id]


# ─── members, unread ─────────────────────────────────────────────────────


def test_members_add_reassign_and_remove(rooms_db) -> None:
    room = _room(rooms_db)

    bob = dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    assert (bob.assigned, bob.added_by) == (False, rooms_db.alice)
    again = dal.add_member(
        rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice, assigned=True
    )
    assert again.assigned is True
    assert dal.set_assigned(rooms_db.tenant, room.id, rooms_db.bob, False).assigned is False
    assert dal.set_assigned(rooms_db.tenant, room.id, rooms_db.carol, True) is None
    assert dal.get_member(rooms_db.tenant, room.id, rooms_db.bob).assigned is False
    assert {m.user_id for m in dal.list_members(rooms_db.tenant, room.id)} == {
        rooms_db.alice,
        rooms_db.bob,
    }

    assert dal.remove_member(rooms_db.tenant, room.id, rooms_db.bob) is True
    assert dal.remove_member(rooms_db.tenant, room.id, rooms_db.bob) is False
    assert dal.get_member(rooms_db.tenant, room.id, rooms_db.bob) is None


def test_set_notify_stores_the_members_preference(rooms_db) -> None:
    room = _room(rooms_db)
    member = dal.set_notify(rooms_db.tenant, room.id, rooms_db.alice, {"edition": "inbox"})
    assert member.notify == {"edition": "inbox"}
    assert dal.set_notify(rooms_db.tenant, room.id, rooms_db.carol, {}) is None


def test_list_rooms_for_user_lists_only_their_rooms(rooms_db) -> None:
    mine = _room(rooms_db, title="Mine")
    shared = _room(rooms_db, title="Shared", created_by=rooms_db.bob)
    dal.add_member(rooms_db.tenant, shared.id, rooms_db.alice, added_by=rooms_db.bob)
    _room(rooms_db, title="Not mine", created_by=rooms_db.bob)

    listed = dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice)

    assert {s.id for s in listed} == {mine.id, shared.id}
    assert {s.title for s in dal.list_all_rooms(rooms_db.tenant)} == {"Mine", "Shared", "Not mine"}


def test_unread_counts_are_per_user(rooms_db) -> None:
    room = _room(rooms_db)
    dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    ids = _messages(rooms_db, room, 3)

    dal.mark_read(rooms_db.tenant, room.id, rooms_db.alice, message_id=ids[1])

    def unread(user: str) -> int:
        (summary,) = dal.list_rooms_for_user(rooms_db.tenant, user)
        return summary.unread

    assert (unread(rooms_db.alice), unread(rooms_db.bob)) == (1, 3)

    edition, _ = dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)
    dal.publish_edition(rooms_db.tenant, edition.id, title="Week 1", summary="Fine", blocks=BLOCKS)
    assert (unread(rooms_db.alice), unread(rooms_db.bob)) == (2, 4)

    dal.mark_read(rooms_db.tenant, room.id, rooms_db.bob)
    assert (unread(rooms_db.alice), unread(rooms_db.bob)) == (2, 0)


def test_mark_read_never_moves_backwards(rooms_db) -> None:
    room = _room(rooms_db)
    ids = _messages(rooms_db, room, 3)
    dal.mark_read(rooms_db.tenant, room.id, rooms_db.alice, message_id=ids[2])
    member = dal.mark_read(rooms_db.tenant, room.id, rooms_db.alice, message_id=ids[0])
    assert member.last_read_message_id == ids[2]


def _unread(db: Any, user: str) -> int:
    (summary,) = dal.list_rooms_for_user(db.tenant, user)
    return summary.unread


def test_a_preview_never_moves_anyones_read_mark(rooms_db) -> None:
    """A preview (built later, so a higher seq) published before a scheduled
    build must not let ``mark_read`` skip that build."""
    room = _room(rooms_db)
    scheduled, _ = dal.create_edition(rooms_db.tenant, room.id)
    preview, _ = dal.create_edition(
        rooms_db.tenant, room.id, preview=True, requested_by=rooms_db.alice
    )
    dal.publish_edition(rooms_db.tenant, preview.id, title="p", summary="p", blocks=BLOCKS)
    assert _unread(rooms_db, rooms_db.alice) == 0

    member = dal.mark_read(rooms_db.tenant, room.id, rooms_db.alice)
    assert member.last_read_published_seq is None
    dal.publish_edition(rooms_db.tenant, scheduled.id, title="s", summary="s", blocks=BLOCKS)

    assert _unread(rooms_db, rooms_db.alice) == 1


def test_editions_published_out_of_order_are_each_unread_once(rooms_db) -> None:
    room = _room(rooms_db)
    first, _ = dal.create_edition(rooms_db.tenant, room.id)
    second, _ = dal.create_edition(rooms_db.tenant, room.id)
    dal.publish_edition(rooms_db.tenant, second.id, title="2", summary="s", blocks=BLOCKS)
    assert _unread(rooms_db, rooms_db.alice) == 1
    dal.mark_read(rooms_db.tenant, room.id, rooms_db.alice)
    assert _unread(rooms_db, rooms_db.alice) == 0

    dal.publish_edition(rooms_db.tenant, first.id, title="1", summary="s", blocks=BLOCKS)

    assert _unread(rooms_db, rooms_db.alice) == 1
    (summary,) = dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice)
    assert summary.latest_edition["id"] == first.id  # the latest to be published


def test_publishing_numbers_editions_in_publish_order_and_skips_previews(rooms_db) -> None:
    room = _room(rooms_db)
    a, _ = dal.create_edition(rooms_db.tenant, room.id)
    b, _ = dal.create_edition(rooms_db.tenant, room.id)
    p, _ = dal.create_edition(rooms_db.tenant, room.id, preview=True, requested_by=rooms_db.bob)

    pb = dal.publish_edition(rooms_db.tenant, b.id, title="b", summary="s", blocks=BLOCKS)
    pp = dal.publish_edition(rooms_db.tenant, p.id, title="p", summary="s", blocks=BLOCKS)
    pa = dal.publish_edition(rooms_db.tenant, a.id, title="a", summary="s", blocks=BLOCKS)

    assert (pb.published_seq, pa.published_seq, pp.published_seq) == (1, 2, None)
    assert (pa.seq, pb.seq) == (a.seq, b.seq)  # build seq is stable


def test_concurrent_publishes_get_distinct_publish_numbers(rooms_db) -> None:
    room = _room(rooms_db)
    made = [dal.create_edition(rooms_db.tenant, room.id)[0] for _ in range(5)]
    it = iter(made)
    lock = threading.Lock()

    def publish() -> Any:
        with lock:
            e = next(it)
        return dal.publish_edition(rooms_db.tenant, e.id, title="t", summary="s", blocks=BLOCKS)

    results = _race(5, publish)

    assert not [r for r in results if isinstance(r, Exception)], results
    assert sorted(r.published_seq for r in results) == [1, 2, 3, 4, 5]


def test_an_explicit_mark_uses_the_publish_number(rooms_db) -> None:
    room = _room(rooms_db)
    first, _ = dal.create_edition(rooms_db.tenant, room.id)
    second, _ = dal.create_edition(rooms_db.tenant, room.id)
    pub2 = dal.publish_edition(rooms_db.tenant, second.id, title="2", summary="s", blocks=BLOCKS)
    dal.publish_edition(rooms_db.tenant, first.id, title="1", summary="s", blocks=BLOCKS)

    member = dal.mark_read(
        rooms_db.tenant, room.id, rooms_db.alice, published_seq=pub2.published_seq
    )

    assert member.last_read_published_seq == 1
    assert _unread(rooms_db, rooms_db.alice) == 1


def test_the_summary_carries_the_latest_published_edition(rooms_db) -> None:
    room = _room(rooms_db)
    first, _ = dal.create_edition(
        rooms_db.tenant,
        room.id,
        grain="week",
        period_start=date(2026, 11, 2),
        period_end=date(2026, 11, 8),
        requested_by=rooms_db.alice,
    )
    dal.publish_edition(rooms_db.tenant, first.id, title="Week 45", summary="ok", blocks=BLOCKS)
    dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)  # still building
    preview, _ = dal.create_edition(
        rooms_db.tenant, room.id, preview=True, requested_by=rooms_db.alice
    )
    dal.publish_edition(rooms_db.tenant, preview.id, title="Preview", summary="p", blocks=BLOCKS)

    (summary,) = dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice)

    assert summary.latest_edition == {
        "id": first.id,
        "seq": first.seq,
        "title": "Week 45",
        "period_start": date(2026, 11, 2),
        "period_end": date(2026, 11, 8),
    }
    assert summary.last_activity_at is not None


def test_listing_rooms_takes_a_bounded_number_of_queries(rooms_db, monkeypatch) -> None:
    executed: list[str] = []

    class _Cursor:
        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *exc: Any) -> None:
            self._real.close()

        def execute(self, query: Any, params: Any = None) -> Any:
            executed.append(str(query))
            return self._real.execute(query, params)

    class _Conn:
        def __init__(self, real: Any) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
            return _Cursor(self._real.cursor(*args, **kwargs))

    @contextlib.contextmanager
    def counting(autocommit: bool = False, read_only: bool = False) -> Any:
        with committed_connection(autocommit=autocommit, read_only=read_only) as conn:
            yield _Conn(conn)

    for i in range(6):
        room = _room(rooms_db, title=f"Room {i}")
        _messages(rooms_db, room, 2)
        edition, _ = dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)
        dal.publish_edition(rooms_db.tenant, edition.id, title="E", summary="s", blocks=BLOCKS)
    monkeypatch.setattr(dal, "get_connection", counting)

    listed = dal.list_rooms_for_user(rooms_db.tenant, rooms_db.alice)

    assert len(listed) == 6
    assert all(s.unread == 3 for s in listed)
    assert len(executed) <= 2, executed


# ─── editions ────────────────────────────────────────────────────────────


def test_editions_take_the_next_seq_and_the_rooms_revision(rooms_db) -> None:
    room = _room(rooms_db)
    dal.update_definition(rooms_db.tenant, room.id, {"title": "B"}, rooms_db.alice, 1)

    seqs = [
        dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)[0]
        for _ in range(3)
    ]

    assert [e.seq for e in seqs] == [1, 2, 3]
    assert all(e.revision == 2 and e.status == "building" for e in seqs)


def test_concurrent_editions_get_distinct_seqs(rooms_db) -> None:
    room = _room(rooms_db)
    results = _race(
        5, lambda: dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)
    )
    assert not [r for r in results if isinstance(r, Exception)], results
    assert sorted(e.seq for e, _ in results) == [1, 2, 3, 4, 5]


def test_two_concurrent_requests_for_one_period_build_one_edition(rooms_db) -> None:
    room = _room(rooms_db)

    def request() -> Any:
        return dal.create_edition(
            rooms_db.tenant,
            room.id,
            grain="week",
            period_start=date(2026, 11, 9),
            period_end=date(2026, 11, 15),
            requested_by=rooms_db.bob,
        )

    results = _race(4, request)

    assert not [r for r in results if isinstance(r, Exception)], results
    assert len({e.id for e, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, False, False, True]
    rooms_db.cur.execute(
        "SELECT COUNT(*) FROM room_editions WHERE tenant_id = %s AND room_id = %s",
        (rooms_db.tenant, room.id),
    )
    assert rooms_db.cur.fetchone()[0] == 1


def test_the_database_itself_refuses_a_second_building_row(rooms_db) -> None:
    room = _room(rooms_db)
    edition, _ = dal.create_edition(
        rooms_db.tenant,
        room.id,
        grain="day",
        period_start=date(2026, 11, 20),
        period_end=date(2026, 11, 20),
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        rooms_db.cur.execute(
            "INSERT INTO room_editions (tenant_id, id, room_id, seq, kind, status, preview, "
            "period_start, period_end, grain, revision) "
            "VALUES (%s, %s, %s, 99, 'edition', 'building', FALSE, %s, %s, 'day', 1)",
            (rooms_db.tenant, str(uuid.uuid4()), room.id, date(2026, 11, 20), date(2026, 11, 20)),
        )
    assert edition.status == "building"


def test_previews_are_never_deduplicated_against_a_real_build(rooms_db) -> None:
    room = _room(rooms_db)
    period = {"grain": "day", "period_start": date(2026, 11, 20), "period_end": date(2026, 11, 20)}
    real, _ = dal.create_edition(rooms_db.tenant, room.id, **period)
    preview, created = dal.create_edition(
        rooms_db.tenant, room.id, preview=True, requested_by=rooms_db.bob, **period
    )
    assert created and preview.id != real.id and preview.preview


def test_a_published_period_can_be_built_again(rooms_db) -> None:
    room = _room(rooms_db)
    period = {"grain": "day", "period_start": date(2026, 11, 20), "period_end": date(2026, 11, 20)}
    first, _ = dal.create_edition(rooms_db.tenant, room.id, **period)
    dal.publish_edition(rooms_db.tenant, first.id, title="t", summary="s", blocks=BLOCKS)
    second, created = dal.create_edition(rooms_db.tenant, room.id, **period)
    assert created and second.id != first.id


def test_publish_stores_the_content_and_refuses_unknown_components(rooms_db) -> None:
    room = _room(rooms_db)
    edition, _ = dal.create_edition(rooms_db.tenant, room.id, run_id="run-1")

    with pytest.raises(InvalidBlocks):
        dal.publish_edition(
            rooms_db.tenant,
            edition.id,
            title="t",
            summary="s",
            blocks=[{"component": "render_iframe", "props": {}}],
        )
    assert dal.get_edition(rooms_db.tenant, edition.id, viewer_id=None).status == "building"

    published = dal.publish_edition(
        rooms_db.tenant,
        edition.id,
        title="Week 46",
        summary="Up 4%",
        blocks=BLOCKS,
        suggestions=[{"key": "k1", "title": "Call Bob", "detail": "", "action_hint": ""}],
        provenance={"run_id": "run-1", "ran_as": rooms_db.alice, "tool_calls": []},
        chat_message_id=7,
    )

    assert published.status == "published"
    assert (published.title, published.summary, published.blocks) == ("Week 46", "Up 4%", BLOCKS)
    assert published.suggestions[0]["key"] == "k1"
    assert published.provenance["run_id"] == "run-1"
    assert published.chat_message_id == 7
    # Publishing is once: a second publish of the same row changes nothing.
    assert (
        dal.publish_edition(rooms_db.tenant, edition.id, title="x", summary="y", blocks=BLOCKS)
        is None
    )


@pytest.mark.parametrize(
    "blocks",
    [
        "not a list",
        [{"props": {}}],
        [{"component": "render_markdown", "props": "x"}],
        [{"component": "render_markdown", "props": {}, "extra": 1}],
    ],
)
def test_publish_refuses_a_malformed_block_list(rooms_db, blocks) -> None:
    room = _room(rooms_db)
    edition, _ = dal.create_edition(rooms_db.tenant, room.id)
    with pytest.raises(InvalidBlocks):
        dal.publish_edition(rooms_db.tenant, edition.id, title="t", summary="s", blocks=blocks)


def test_fail_records_the_error_and_a_failed_edition_cannot_publish(rooms_db) -> None:
    room = _room(rooms_db)
    edition, _ = dal.create_edition(rooms_db.tenant, room.id)

    failed = dal.fail_edition(rooms_db.tenant, edition.id, "the tool timed out")

    assert (failed.status, failed.error) == ("failed", "the tool timed out")
    assert (
        dal.publish_edition(rooms_db.tenant, edition.id, title="t", summary="s", blocks=BLOCKS)
        is None
    )


def test_previews_are_hidden_from_everyone_but_the_requester(rooms_db) -> None:
    room = _room(rooms_db)
    real, _ = dal.create_edition(rooms_db.tenant, room.id, requested_by=rooms_db.alice)
    preview, _ = dal.create_edition(
        rooms_db.tenant, room.id, preview=True, requested_by=rooms_db.alice
    )
    for e in (real, preview):
        dal.publish_edition(rooms_db.tenant, e.id, title="t", summary="s", blocks=BLOCKS)

    def ids(viewer: str | None) -> list[str]:
        return [e.id for e in dal.list_editions(rooms_db.tenant, room.id, viewer_id=viewer)]

    assert set(ids(rooms_db.alice)) == {real.id, preview.id}
    assert ids(rooms_db.bob) == [real.id]
    assert ids(None) == [real.id]
    assert dal.get_edition(rooms_db.tenant, preview.id, viewer_id=rooms_db.bob) is None
    assert dal.get_edition(rooms_db.tenant, preview.id, viewer_id=rooms_db.alice).id == preview.id


def test_list_editions_shows_published_newest_first_unless_asked(rooms_db) -> None:
    room = _room(rooms_db)
    made = [dal.create_edition(rooms_db.tenant, room.id)[0] for _ in range(3)]
    for e in made[:2]:
        dal.publish_edition(rooms_db.tenant, e.id, title="t", summary="s", blocks=BLOCKS)

    published = dal.list_editions(rooms_db.tenant, room.id, viewer_id=None)
    everything = dal.list_editions(
        rooms_db.tenant, room.id, viewer_id=None, include_unpublished=True
    )

    assert [e.seq for e in published] == [2, 1]
    assert [e.seq for e in everything] == [3, 2, 1]


def test_find_edition_for_period_prefers_the_published_one(rooms_db) -> None:
    room = _room(rooms_db)
    period = ("week", date(2026, 11, 9), date(2026, 11, 15))
    assert dal.find_edition_for_period(rooms_db.tenant, room.id, *period) is None
    building, _ = dal.create_edition(
        rooms_db.tenant, room.id, grain="week", period_start=period[1], period_end=period[2]
    )
    assert dal.find_edition_for_period(rooms_db.tenant, room.id, *period).id == building.id
    dal.publish_edition(rooms_db.tenant, building.id, title="t", summary="s", blocks=BLOCKS)
    rebuild, _ = dal.create_edition(
        rooms_db.tenant, room.id, grain="week", period_start=period[1], period_end=period[2]
    )
    found = dal.find_edition_for_period(rooms_db.tenant, room.id, *period)
    assert found.id == building.id and rebuild.id != building.id


# ─── tasks ───────────────────────────────────────────────────────────────


def test_link_is_idempotent(rooms_db) -> None:
    room = _room(rooms_db)
    task = _task(rooms_db)
    first = dal.link_task(rooms_db.tenant, room.id, task, origin="suggestion", suggestion_key="k1")
    again = dal.link_task(rooms_db.tenant, room.id, task)
    assert (
        (again.origin, again.suggestion_key)
        == ("suggestion", "k1")
        == (
            first.origin,
            first.suggestion_key,
        )
    )
    assert [t.task_id for t in dal.list_room_tasks(rooms_db.tenant, room.id)] == [task]


def test_a_link_refuses_an_unknown_origin(rooms_db) -> None:
    room = _room(rooms_db)
    with pytest.raises(ValueError):
        dal.link_task(rooms_db.tenant, room.id, _task(rooms_db), origin="magic")


def test_a_task_is_accepted_once(rooms_db) -> None:
    room = _room(rooms_db)
    task = _task(rooms_db)
    dal.link_task(rooms_db.tenant, room.id, task, origin="suggestion")

    accepted = dal.accept_task(rooms_db.tenant, room.id, task, rooms_db.bob)

    assert accepted.accepted_by == rooms_db.bob
    assert dal.accept_task(rooms_db.tenant, room.id, task, rooms_db.alice) is None
    assert dal.dismiss_task(rooms_db.tenant, room.id, task, rooms_db.alice) is None
    (row,) = dal.list_room_tasks(rooms_db.tenant, room.id)
    assert (row.accepted_by, row.dismissed_by) == (rooms_db.bob, None)


def test_concurrent_accepts_have_one_winner(rooms_db) -> None:
    room = _room(rooms_db)
    task = _task(rooms_db)
    dal.link_task(rooms_db.tenant, room.id, task)
    results = _race(4, lambda: dal.accept_task(rooms_db.tenant, room.id, task, rooms_db.bob))
    assert sum(r is not None for r in results) == 1


def test_a_dismissed_task_cannot_be_accepted(rooms_db) -> None:
    room = _room(rooms_db)
    task = _task(rooms_db)
    dal.link_task(rooms_db.tenant, room.id, task)
    assert dal.dismiss_task(rooms_db.tenant, room.id, task, rooms_db.bob).dismissed_by == (
        rooms_db.bob
    )
    assert dal.accept_task(rooms_db.tenant, room.id, task, rooms_db.bob) is None
    assert dal.accept_task(rooms_db.tenant, room.id, _task(rooms_db), rooms_db.bob) is None


def test_get_or_create_task_room_makes_one_task_room(rooms_db) -> None:
    task = _task(rooms_db, title="Chase the invoice")

    room = dal.get_or_create_task_room(
        rooms_db.tenant, task, agent_id="main", created_by=rooms_db.alice
    )

    assert (room.kind, room.task_id, room.title) == ("task", task, "Chase the invoice")
    (link,) = dal.list_room_tasks(rooms_db.tenant, room.id)
    assert (link.task_id, link.origin) == (task, "subject")
    again = dal.get_or_create_task_room(
        rooms_db.tenant, task, agent_id="main", created_by=rooms_db.bob
    )
    assert again.id == room.id


def test_a_racing_get_or_create_makes_one_room(rooms_db) -> None:
    task = _task(rooms_db)

    results = _race(
        5,
        lambda: dal.get_or_create_task_room(
            rooms_db.tenant, task, agent_id="main", created_by=rooms_db.alice
        ),
    )

    assert not [r for r in results if isinstance(r, Exception)], results
    assert len({r.id for r in results}) == 1
    rooms_db.cur.execute(
        "SELECT COUNT(*) FROM rooms WHERE tenant_id = %s AND task_id = %s", (rooms_db.tenant, task)
    )
    assert rooms_db.cur.fetchone()[0] == 1
    rooms_db.cur.execute(
        "SELECT COUNT(*) FROM room_revisions WHERE tenant_id = %s AND room_id = %s",
        (rooms_db.tenant, results[0].id),
    )
    assert rooms_db.cur.fetchone()[0] == 1


def test_a_task_of_another_tenant_gets_no_room(rooms_db) -> None:
    task = _task(rooms_db, tenant=rooms_db.other)
    assert (
        dal.get_or_create_task_room(
            rooms_db.tenant, task, agent_id="main", created_by=rooms_db.alice
        )
        is None
    )
    assert (
        dal.get_or_create_task_room(
            rooms_db.tenant, "not-a-uuid", agent_id="main", created_by=rooms_db.alice
        )
        is None
    )


# ─── the company boundary ────────────────────────────────────────────────


def test_another_tenants_ids_return_nothing(rooms_db) -> None:
    room = _room(rooms_db)
    edition, _ = dal.create_edition(rooms_db.tenant, room.id)
    task = _task(rooms_db)
    dal.link_task(rooms_db.tenant, room.id, task)
    other = rooms_db.other

    assert dal.get_room(other, room.id) is None
    assert dal.get_member(other, room.id, rooms_db.alice) is None
    assert dal.list_members(other, room.id) == []
    assert dal.list_revisions(other, room.id) == []
    assert dal.list_rooms_for_user(other, rooms_db.alice) == []
    assert dal.list_all_rooms(other) == []
    assert dal.get_edition(other, edition.id, viewer_id=None) is None
    assert dal.list_editions(other, room.id, viewer_id=None, include_unpublished=True) == []
    assert dal.list_room_tasks(other, room.id) == []
    assert dal.accept_task(other, room.id, task, rooms_db.bob) is None
    assert dal.dismiss_task(other, room.id, task, rooms_db.bob) is None
    assert dal.publish_edition(other, edition.id, title="t", summary="s", blocks=BLOCKS) is None
    assert dal.fail_edition(other, edition.id, "x") is None
    assert dal.archive_room(other, room.id) is None
    assert dal.unarchive_room(other, room.id) is None
    assert dal.remove_member(other, room.id, rooms_db.alice) is False
    assert dal.set_assigned(other, room.id, rooms_db.alice, False) is None
    assert dal.mark_read(other, room.id, rooms_db.alice) is None


@pytest.mark.parametrize(
    "call",
    [
        lambda db, room: dal.update_definition(db.other, room.id, {"title": "x"}, db.bob, 1),
        lambda db, room: dal.restore_revision(db.other, room.id, 1, db.bob),
        lambda db, room: dal.duplicate_room(db.other, room.id, db.bob),
        lambda db, room: dal.add_member(db.other, room.id, db.bob, added_by=db.bob),
        lambda db, room: dal.create_edition(db.other, room.id),
        lambda db, room: dal.link_task(db.other, room.id, str(uuid.uuid4())),
    ],
)
def test_writes_against_another_tenants_room_are_not_found(rooms_db, call) -> None:
    room = _room(rooms_db)
    with pytest.raises(RoomNotFound):
        call(rooms_db, room)
    assert dal.list_members(rooms_db.tenant, room.id)[0].user_id == rooms_db.alice
    assert dal.get_room(rooms_db.tenant, room.id).rev == 1
