"""A workspace run cannot start workspace work; a person's turn can.

A run that starts runs is a loop, not usage. So ``room_run`` and
``room_request_period`` refuse when the calling run is itself a workspace run
(a scheduled, on-demand, period or preview run: its room carrier is
``unattended``) or a sub-agent run, and every room tool but ``room_read``
refuses inside a workspace run, so a run cannot create scheduled workspaces
either. A person's interactive turn in a workspace (an attended carrier) and
the main conversation (no carrier) still start runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.rooms import editions, periods, runs
from robothor.rooms.access import room_session_key
from robothor.rooms.models import Edition
from robothor.rooms.run_context import RoomRun, room_run_scope

ROOM = str(uuid.uuid4())
ALICE = str(uuid.uuid4())
KEY = room_session_key("main", ROOM)


def _edition() -> Edition:
    return Edition(
        tenant_id="test-tenant",
        id=str(uuid.uuid4()),
        room_id=ROOM,
        seq=1,
        kind="edition",
        status="building",
        preview=False,
        period_start=date(2026, 11, 20),
        period_end=date(2026, 11, 20),
        grain="day",
        revision=1,
        created_at=datetime(2026, 11, 21, tzinfo=UTC),
    )


@pytest.fixture
def started(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def start_room_run(actor: Any, room_id: str, **kw: Any) -> Any:
        calls.append("room_run")
        return _edition(), False

    async def request_period(actor: Any, room_id: str, grain: str, anchor: Any) -> Any:
        calls.append("room_request_period")
        return _edition(), True

    monkeypatch.setattr(runs, "start_room_run", start_room_run)
    monkeypatch.setattr(periods, "request_period", request_period)
    monkeypatch.setattr(editions, "load_run_record", lambda tenant, run_id: None)
    return calls


def _ctx(run_id: str = "") -> ToolContext:
    return ToolContext(
        tenant_id="test-tenant", user_id=ALICE, user_role="member", agent_id="main", run_id=run_id
    )


async def _run_tool(name: str, ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.handlers.room_periods import HANDLERS as PERIODS
    from robothor.engine.tools.handlers.room_runs import HANDLERS as RUNS

    if name == "room_run":
        return await RUNS["room_run"]({"room_id": ROOM}, ctx)
    return await PERIODS["room_request_period"](
        {"room_id": ROOM, "grain": "day", "anchor": "2026-11-20"}, ctx
    )


def _unattended(**kw: Any) -> RoomRun:
    run = RoomRun.for_session("test-tenant", KEY, unattended=True, **kw)
    assert run is not None
    return run


TOOLS = ["room_run", "room_request_period"]


@pytest.mark.parametrize("name", TOOLS)
async def test_a_workspace_run_cannot_start_workspace_work(started, name):
    with room_run_scope(_unattended(edition_id=str(uuid.uuid4()))):
        result = await _run_tool(name, _ctx())
    assert "error" in result and started == []


@pytest.mark.parametrize("name", TOOLS)
async def test_a_workspace_run_with_no_edition_cannot_either(started, name):
    """A scheduled run of a room that is not a report has no edition, and is still a run."""
    with room_run_scope(_unattended()):
        result = await _run_tool(name, _ctx())
    assert "error" in result and started == []


@pytest.mark.parametrize("name", TOOLS)
async def test_a_sub_agent_run_cannot_start_workspace_work(started, monkeypatch, name):
    record = editions.RunRecord(started_at=None, is_child=True, steps=[])
    monkeypatch.setattr(editions, "load_run_record", lambda tenant, run_id: record)

    result = await _run_tool(name, _ctx(run_id=str(uuid.uuid4())))

    assert "error" in result and started == []


@pytest.mark.parametrize("name", TOOLS)
async def test_a_persons_turn_in_the_workspace_still_can(started, name):
    attended = RoomRun.for_session("test-tenant", KEY)
    with room_run_scope(attended):
        result = await _run_tool(name, _ctx(run_id=str(uuid.uuid4())))
    assert "error" not in result and started == [name]


@pytest.mark.parametrize("name", TOOLS)
async def test_the_main_conversation_still_can(started, name):
    result = await _run_tool(name, _ctx())
    assert "error" not in result and started == [name]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            "room_create",
            {"kind": "report", "title": "T", "instructions": "I", "schedule": "0 * * * *"},
        ),
        (
            "room_update",
            {"room_id": ROOM, "expected_rev": 1, "changes": {"schedule_text": "0 * * * *"}},
        ),
        ("room_duplicate", {"room_id": ROOM}),
        ("room_share", {"room_id": ROOM, "user": "bob@example.com"}),
    ],
)
async def test_other_room_tools_refuse_inside_a_workspace_run(monkeypatch, tool, args):
    from robothor.engine.tools.handlers import rooms
    from robothor.rooms import definition

    def must_not_run(*a: Any, **k: Any) -> Any:
        raise AssertionError("the definition layer was reached from inside a workspace run")

    for name in (
        "create_room_with_schedule",
        "apply_definition_change",
        "duplicate_room",
        "share_room",
    ):
        monkeypatch.setattr(definition, name, must_not_run)

    with room_run_scope(_unattended()):
        result = await rooms.HANDLERS[tool](args, _ctx())

    assert "error" in result


def test_the_carrier_says_whether_a_person_is_behind_it():
    assert RoomRun.for_session("test-tenant", KEY).unattended is False
    assert _unattended().unattended is True
