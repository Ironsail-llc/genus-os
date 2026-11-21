"""Helpers for the rooms performance tests: statement counting and bulk seeding.

``counting_connection(log)`` has the shape of ``get_connection`` (and of the
rooms fixtures' ``committed_connection``, which it wraps): every connection it
yields hands out cursors whose ``execute`` / ``executemany`` append the
statement to ``log`` before running it. Patch it over the ``get_connection``
name a module imported, as the fixtures patch ``committed_connection``.

``seed_rooms`` writes rooms in bulk with the rows ``dal.create_room``,
``publish_edition`` and the chat store would leave (session key, assigned
creator, a published edition with ``published_seq``), and moves the shared
member's read marks through ``dal.mark_read`` itself, so the unread counts the
tests expect come from the real mark semantics.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg2.extras import Json, execute_values

from robothor.rooms import dal
from robothor.rooms.keys import room_session_key
from robothor.rooms.tests.conftest import committed_connection

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class _Cursor:
    def __init__(self, real: Any, log: list[str]) -> None:
        self._real = real
        self._log = log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._real)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def execute(self, query: Any, params: Any = None) -> Any:
        self._log.append(" ".join(str(query).split()))
        return self._real.execute(query, params)

    def executemany(self, query: Any, params: Any) -> Any:
        self._log.append(" ".join(str(query).split()))
        return self._real.executemany(query, params)


class _Conn:
    def __init__(self, real: Any, log: list[str]) -> None:
        self._real = real
        self._log = log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor(self._real.cursor(*args, **kwargs), self._log)


def counting_connection(log: list[str]) -> Callable[..., Any]:
    """A ``get_connection`` stand-in that records every statement into ``log``."""

    @contextlib.contextmanager
    def connection(autocommit: bool = False, read_only: bool = False) -> Iterator[Any]:
        with committed_connection(autocommit=autocommit, read_only=read_only) as conn:
            yield _Conn(conn, log)

    return connection


@dataclass(frozen=True)
class Seeded:
    """One seeded room and the unread count its shared member should see."""

    index: int
    room_id: str
    title: str
    edition_id: str
    unread: int


def seed_rooms(
    cur: Any, tenant: str, *, owner: str, reader: str, start: int, count: int
) -> list[Seeded]:
    """``count`` report rooms numbered from ``start``: ``owner`` assigned,
    ``reader`` shared, 1-4 messages each and one published edition.

    Room ``i`` is ``i`` minutes old, so the newest-first listing is by index.
    The reader's marks vary with ``i``: every fifth room read up to its first
    message and the edition, every seventh (not fifth) read to the end with
    ``mark_read()``, the rest unread; an edition that arrived as a chat message
    (every third room) is never counted on top of its message.
    """
    indexes = range(start, start + count)
    ids = {i: str(uuid.uuid4()) for i in indexes}
    keys = {i: room_session_key("main", ids[i]) for i in indexes}
    execute_values(
        cur,
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by, "
        "run_as_user_id, instructions, settings, last_activity_at) VALUES %s",
        [
            (
                tenant,
                ids[i],
                "report",
                f"Room {i:03d}",
                "main",
                keys[i],
                owner,
                owner,
                "Summarise the week.",
                Json({"tz": "UTC", "default_grain": "week"}),
                f"-{i} minutes",
            )
            for i in indexes
        ],
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + %s::interval)",
    )
    execute_values(
        cur,
        "INSERT INTO room_revisions (tenant_id, room_id, rev, changed_by, definition) VALUES %s",
        [(tenant, ids[i], 1, owner, Json({"title": f"Room {i:03d}"})) for i in indexes],
    )
    execute_values(
        cur,
        "INSERT INTO room_members (tenant_id, room_id, user_id, assigned, added_by) VALUES %s",
        [(tenant, ids[i], owner, True, owner) for i in indexes]
        + [(tenant, ids[i], reader, False, owner) for i in indexes],
    )
    sessions = execute_values(
        cur,
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES %s "
        "RETURNING session_key, id",
        [(tenant, keys[i], "webchat") for i in indexes],
        fetch=True,
    )
    session_of = dict(sessions)
    by_key = {keys[i]: i for i in indexes}
    rows = [
        (
            session_of[keys[i]],
            Json(
                {
                    "role": "user",
                    "content": f"room {i} message {n}",
                    "author_user_id": owner,
                    "author_display_name": "Alice",
                }
            ),
        )
        for i in indexes
        for n in range(1 + i % 4)
    ]
    inserted = execute_values(
        cur,
        "INSERT INTO chat_messages (session_id, message) VALUES %s RETURNING session_id, id",
        rows,
        fetch=True,
        page_size=len(rows),
    )
    room_of_session = {sid: by_key[key] for key, sid in session_of.items()}
    message_ids: dict[int, list[int]] = {i: [] for i in indexes}
    for session_id, message_id in inserted:
        message_ids[room_of_session[session_id]].append(int(message_id))
    for found in message_ids.values():
        found.sort()
    editions = {i: str(uuid.uuid4()) for i in indexes}
    execute_values(
        cur,
        "INSERT INTO room_editions (tenant_id, id, room_id, seq, kind, status, preview, "
        "revision, published_seq, title, summary, chat_message_id) VALUES %s",
        [
            (
                tenant,
                editions[i],
                ids[i],
                1,
                "edition",
                "published",
                False,
                1,
                1,
                f"Edition {i}",
                "Up 4%.",
                message_ids[i][-1] if i % 3 == 0 else None,
            )
            for i in indexes
        ],
    )

    seeded = []
    for i in indexes:
        messages = len(message_ids[i])
        edition_unread = 0 if i % 3 == 0 else 1
        if i % 5 == 0:
            dal.mark_read(tenant, ids[i], reader, message_id=message_ids[i][0], published_seq=1)
            unread = messages - 1
        elif i % 7 == 0:
            dal.mark_read(tenant, ids[i], reader)
            unread = 0
        else:
            unread = messages + edition_unread
        seeded.append(Seeded(i, ids[i], f"Room {i:03d}", editions[i], unread))
    return seeded
