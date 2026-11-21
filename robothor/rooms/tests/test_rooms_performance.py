"""Story I6: the thread list with unread for 200 threads stays within budget.

Against real Postgres, one tenant with 200 rooms, Bob shared into every one,
each with a few chat messages and one published edition:

- ``list_rooms_for_user`` returns all 200 with Bob's unread counts, in one
  statement whatever the room count (20 rooms and 200 rooms cost the same
  number of statements), well under two seconds;
- the admin view (``list_all_rooms`` + ``members_of_rooms``) is two
  statements at either size, with every room's members;
- ``list_editions`` over a room with 300 editions honours its limit, newest
  first, published only (and only the viewer's own previews), in one
  statement whatever the room's edition count.

The wall-clock bounds are deliberately generous: they catch a per-room query
or an unindexed scan, not ordinary jitter.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from psycopg2.extras import execute_values

from robothor.rooms import dal
from robothor.rooms.tests.perf import counting_connection, seed_rooms

pytestmark = pytest.mark.integration

LIST_BUDGET_S = 2.0


def _measure(monkeypatch: Any, fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, list[str], float]:
    """``fn``'s result, the statements it executed, and its wall-clock seconds
    (printed, for ``pytest -s``)."""
    log: list[str] = []
    with monkeypatch.context() as patched:
        patched.setattr(dal, "get_connection", counting_connection(log))
        started = time.perf_counter()
        result = fn(*args, **kwargs)
        elapsed = time.perf_counter() - started
    size = len(result[0]) if isinstance(result, tuple) else len(result)
    print(f"I6 {fn.__name__}: {size} rows, {len(log)} statements, {elapsed * 1000:.1f} ms")
    return result, log, elapsed


def _admin_view(tenant: str) -> tuple[list[Any], dict[str, list[Any]]]:
    rooms = dal.list_all_rooms(tenant, include_archived=True)
    return rooms, dal.members_of_rooms(tenant, [s.id for s in rooms])


# ─── the thread list ─────────────────────────────────────────────────────


def test_bobs_thread_list_of_200_is_one_statement_and_fast(rooms_db, monkeypatch) -> None:
    db = rooms_db
    small = seed_rooms(db.cur, db.tenant, owner=db.alice, reader=db.bob, start=0, count=20)

    listed_20, log_20, _ = _measure(monkeypatch, dal.list_rooms_for_user, db.tenant, db.bob)
    assert len(listed_20) == 20
    assert {s.id: s.unread for s in listed_20} == {r.room_id: r.unread for r in small}

    seeded = small + seed_rooms(
        db.cur, db.tenant, owner=db.alice, reader=db.bob, start=20, count=180
    )
    listed, log_200, elapsed = _measure(monkeypatch, dal.list_rooms_for_user, db.tenant, db.bob)

    assert len(listed) == 200
    assert [s.id for s in listed] == [r.room_id for r in seeded]  # newest first
    assert {s.id: s.unread for s in listed} == {r.room_id: r.unread for r in seeded}
    assert {s.unread for s in listed} >= {0, 1, 2, 3, 4, 5}  # every mark shape is exercised
    by_id = {r.room_id: r for r in seeded}
    for summary in listed:
        assert summary.title == by_id[summary.id].title
        assert summary.latest_edition is not None
        assert summary.latest_edition["id"] == by_id[summary.id].edition_id
        assert summary.latest_edition["published_seq"] == 1
    assert len(log_20) == len(log_200) == 1, log_200
    assert elapsed < LIST_BUDGET_S, f"200-room list took {elapsed:.3f}s"


def test_a_room_list_belongs_to_its_reader_and_tenant(rooms_db, monkeypatch) -> None:
    """The 200 rooms are Bob's and Alice's; Carol and the other tenant see none."""
    db = rooms_db
    seed_rooms(db.cur, db.tenant, owner=db.alice, reader=db.bob, start=0, count=200)

    alice, log, elapsed = _measure(monkeypatch, dal.list_rooms_for_user, db.tenant, db.alice)

    assert len(alice) == 200
    # Alice never moved a mark: every message is unread, and every edition that
    # did not arrive as a chat message.
    assert all(s.unread >= 1 for s in alice)
    assert len(log) == 1
    assert elapsed < LIST_BUDGET_S
    assert dal.list_rooms_for_user(db.tenant, db.carol) == []
    assert dal.list_rooms_for_user(db.other, db.bob) == []


# ─── the admin view ──────────────────────────────────────────────────────


def test_the_admin_view_of_200_rooms_is_two_statements_and_fast(rooms_db, monkeypatch) -> None:
    db = rooms_db
    seed_rooms(db.cur, db.tenant, owner=db.alice, reader=db.bob, start=0, count=20)
    (rooms_20, members_20), log_20, _ = _measure(monkeypatch, _admin_view, db.tenant)
    assert len(rooms_20) == len(members_20) == 20

    seed_rooms(db.cur, db.tenant, owner=db.alice, reader=db.bob, start=20, count=180)
    (rooms, members), log_200, elapsed = _measure(monkeypatch, _admin_view, db.tenant)

    assert len(rooms) == 200
    assert all(s.unread == 0 for s in rooms)  # no reader in the admin view
    assert set(members) == {s.id for s in rooms}
    for room_members in members.values():
        assert [(m.user_id, m.assigned) for m in room_members] == [
            (db.alice, True),
            (db.bob, False),
        ]
    assert len(log_20) == len(log_200) == 2, log_200
    assert elapsed < LIST_BUDGET_S, f"200-room admin view took {elapsed:.3f}s"


# ─── editions ────────────────────────────────────────────────────────────


def _seed_editions(db: Any, room_id: str, count: int) -> list[tuple[int, str, bool]]:
    """``count`` editions by build order: every 25th a preview of Alice's,
    every other tenth failed, the rest published in build order.
    ``[(seq, status, preview)]``."""
    rows = []
    published = 0
    for seq in range(1, count + 1):
        preview = seq % 25 == 0
        failed = not preview and seq % 10 == 0
        status = "failed" if failed else "published"
        published_seq = None
        if not preview and not failed:
            published += 1
            published_seq = published
        rows.append(
            (
                db.tenant,
                str(uuid.uuid4()),
                room_id,
                seq,
                status,
                preview,
                1,
                published_seq,
                f"Edition {seq}",
                db.alice if preview else None,
            )
        )
    execute_values(
        db.cur,
        "INSERT INTO room_editions (tenant_id, id, room_id, seq, status, preview, revision, "
        "published_seq, title, requested_by) VALUES %s",
        rows,
    )
    return [(r[3], r[4], r[5]) for r in rows]


def _expected(
    seeded: list[tuple[int, str, bool]], *, own_previews: bool, unpublished: bool, limit: int
) -> list[int]:
    """The seqs ``list_editions`` should answer, newest first."""
    visible = [
        seq
        for seq, status, preview in seeded
        if (unpublished or status == "published") and (own_previews or not preview)
    ]
    return sorted(visible, reverse=True)[:limit]


def _room(db: Any, title: str) -> Any:
    room = dal.create_room(
        db.tenant, kind="report", title=title, agent_id="main", created_by=db.alice
    )
    dal.add_member(db.tenant, room.id, db.bob, added_by=db.alice)
    return room


def test_editions_of_a_300_edition_room_page_by_limit_in_one_statement(
    rooms_db, monkeypatch
) -> None:
    db = rooms_db
    small, big = _room(db, "Small"), _room(db, "Big")
    _seed_editions(db, small.id, 30)
    seeded = _seed_editions(db, big.id, 300)

    _, log_small, _ = _measure(
        monkeypatch, dal.list_editions, db.tenant, small.id, viewer_id=db.bob, limit=50
    )
    first, log_big, elapsed = _measure(
        monkeypatch, dal.list_editions, db.tenant, big.id, viewer_id=db.bob, limit=50
    )

    assert [e.seq for e in first] == _expected(
        seeded, own_previews=False, unpublished=False, limit=50
    )
    assert first[0].seq == 299  # 300 is Alice's preview
    assert all(e.status == "published" and not e.preview for e in first)
    assert len(log_small) == len(log_big) == 1
    assert elapsed < LIST_BUDGET_S

    # The router's largest page: 200 rows, still one statement and fast.
    widest, log_wide, elapsed_wide = _measure(
        monkeypatch, dal.list_editions, db.tenant, big.id, viewer_id=db.bob, limit=200
    )
    assert [e.seq for e in widest] == _expected(
        seeded, own_previews=False, unpublished=False, limit=200
    )
    assert len(log_wide) == 1
    assert elapsed_wide < LIST_BUDGET_S

    # Alice sees her own previews among the published; building/failed rows
    # only with include_unpublished.
    mine = dal.list_editions(db.tenant, big.id, viewer_id=db.alice, limit=200)
    assert [e.seq for e in mine] == _expected(
        seeded, own_previews=True, unpublished=False, limit=200
    )
    assert any(e.preview for e in mine)
    everything = dal.list_editions(
        db.tenant, big.id, viewer_id=db.bob, include_unpublished=True, limit=300
    )
    assert [e.seq for e in everything] == _expected(
        seeded, own_previews=False, unpublished=True, limit=300
    )
    assert len(everything) == 300 - 12  # Bob never sees Alice's 12 previews
    assert sum(e.status == "failed" for e in everything) == 24
