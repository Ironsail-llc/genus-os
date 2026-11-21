"""The periods route's edges, against real Postgres (story C3).

- A shared member asking a PAUSED workspace for a period is refused 409
  ``paused``, and nothing is inserted or started: a paused workspace runs on
  demand only for the people who manage it, the same rule as run now.
- A custom period at the very start of the calendar, in a time zone east of
  UTC, is refused 422 ``invalid_period``, never an unhandled error.

The engine fixture (real chat and periods routes, real rooms database, a
stand-in background run) comes from ``test_rooms_engine_contract.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.tests.test_rooms_engine_contract import _human, engine  # noqa: F401
from robothor.rooms import dal
from robothor.rooms.tests.conftest import rooms_db  # noqa: F401

pytestmark = pytest.mark.integration


def _editions(s: Any) -> list[Any]:
    return dal.list_editions(s.db.tenant, s.room.id, viewer_id=None, include_unpublished=True)


async def test_a_shared_member_cannot_request_a_period_of_a_paused_workspace(engine) -> None:  # noqa: F811
    s = engine
    dal.set_paused(s.db.tenant, s.room.id, True)
    before = _editions(s)
    with _human(s.db, s.db.bob):
        answer = await s.client.post(
            f"/api/rooms/{s.room.id}/periods", json={"grain": "week", "anchor": "2026-10-28"}
        )
    assert answer.status_code == 409, answer.text
    assert answer.json()["error"] == "paused"
    assert _editions(s) == before
    assert s.started == []


@pytest.mark.parametrize("tz", ["Australia/Sydney", "Asia/Tokyo"])
@pytest.mark.parametrize(
    ("grain", "anchor"),
    [("custom", "0001-01-01/0001-01-03"), ("day", "0001-01-01"), ("week", "0001-01-01")],
)
async def test_a_period_at_the_start_of_the_calendar_is_refused_cleanly(
    engine,  # noqa: F811
    tz: str,
    grain: str,
    anchor: str,
) -> None:
    s = engine
    room = dal.create_room(
        s.db.tenant,
        kind="report",
        title="East of UTC",
        agent_id="main",
        created_by=s.db.alice,
        settings={"tz": tz, "default_grain": "day"},
    )
    dal.add_member(s.db.tenant, room.id, s.db.bob, added_by=s.db.alice)
    with _human(s.db, s.db.bob):
        answer = await s.client.post(
            f"/api/rooms/{room.id}/periods", json={"grain": grain, "anchor": anchor}
        )
    assert answer.status_code == 422, answer.text
    assert answer.json()["error"] == "invalid_period"
    assert s.started == []
