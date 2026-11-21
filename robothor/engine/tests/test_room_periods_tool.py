"""``room_request_period``: an agent asks a report room for a period.

A thin wrapper over ``robothor.rooms.periods.request_period`` (proven against
real Postgres in ``robothor/rooms/tests``). The caller is the human the run is
for, taken from the tool context and never from the arguments; the result
carries the contract's ``Edition`` and the room's link; a refusal is an
``error`` the agent can repeat to the person, not an exception.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.models import Edition
from robothor.rooms.periods import PeriodError, RoomRunUnavailable

ROOM = "7a0c0e8e-63c4-4c5b-9a43-0d6f6f0c2b11"
ALICE = "5b8e4a8a-0f6e-4e43-8f53-1f9d1d3c2a77"


def _edition(status: str = "building") -> Edition:
    return Edition(
        tenant_id="test-tenant",
        id="0d1c7a3e-8a4b-4f0e-9c55-6a3d7c1e2f90",
        room_id=ROOM,
        seq=1,
        kind="edition",
        status=status,
        preview=False,
        period_start=date(2026, 11, 20),
        period_end=date(2026, 11, 20),
        grain="day",
        revision=1,
        created_at=datetime(2026, 11, 21, 9, 0, tzinfo=UTC),
    )


class FakeRequest:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result: Any = (_edition(), True)
        self.error: Exception | None = None

    async def __call__(self, actor: Any, room_id: str, grain: str, anchor: Any) -> Any:
        self.calls.append((actor, room_id, grain, anchor))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake(monkeypatch):
    fake = FakeRequest()
    monkeypatch.setattr("robothor.rooms.periods.request_period", fake)
    return fake


def _ctx(**overrides: Any) -> ToolContext:
    fields: dict[str, Any] = {
        "agent_id": "main",
        "tenant_id": "test-tenant",
        "user_id": ALICE,
        "user_role": "member",
    }
    fields.update(overrides)
    return ToolContext(**fields)


async def _call(args: dict[str, Any], ctx: ToolContext | None = None) -> dict[str, Any]:
    from robothor.engine.tools.handlers.room_periods import HANDLERS

    return await HANDLERS["room_request_period"](args, ctx or _ctx())


async def test_a_started_run_returns_the_edition_and_the_link(fake):
    result = await _call({"room_id": ROOM, "grain": "day", "anchor": "2026-11-20"})

    assert result["started"] is True
    assert result["edition"]["periodStart"] == "2026-11-20"
    assert result["edition"]["status"] == "building"
    assert result["link"] == f"/?v=chat&room={ROOM}"
    assert fake.calls[0][1:] == (ROOM, "day", "2026-11-20")


async def test_an_existing_edition_is_returned_unstarted(fake):
    fake.result = (_edition("published"), False)

    result = await _call({"room_id": ROOM, "grain": "day", "anchor": "2026-11-20"})

    assert result["started"] is False
    assert result["edition"]["status"] == "published"


async def test_the_caller_is_the_runs_human_not_the_arguments(fake):
    await _call(
        {"room_id": ROOM, "grain": "day", "user_id": "someone-else", "role": "owner"},
        _ctx(user_role="viewer"),
    )

    actor = fake.calls[0][0]
    assert (actor.user_id, actor.role, actor.tenant_id) == (ALICE, "viewer", "test-tenant")


async def test_a_run_with_no_human_is_refused(fake):
    result = await _call({"room_id": ROOM, "grain": "day"}, _ctx(user_id=""))

    assert "error" in result
    assert fake.calls == []


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (RoomNotFound(ROOM), "not found"),
        (RoomForbidden("viewer access does not allow 'request_period'"), "not allow"),
        (PeriodError("that day is not over yet"), "not over"),
        (RoomRunUnavailable("room runs are not available"), "not available"),
    ],
)
async def test_a_refusal_is_an_error_result(fake, error, needle):
    fake.error = error

    result = await _call({"room_id": ROOM, "grain": "day"})

    assert needle in result["error"]
    assert "edition" not in result


async def test_a_started_result_carries_no_limit_fields(fake):
    result = await _call({"room_id": ROOM, "grain": "day", "anchor": "2026-11-20"})

    assert not {"cap_reached", "limit", "used"} & set(result)


def test_the_tool_description_promises_no_limit():
    from robothor.engine.tools.schemas import get_engine_schemas

    description = get_engine_schemas()["room_request_period"]["function"]["description"]
    assert "cap" not in description.lower()


@pytest.mark.parametrize(
    "args", [{"grain": "day"}, {"room_id": ROOM}, {"room_id": 3, "grain": "day"}]
)
async def test_missing_arguments_are_refused(fake, args):
    result = await _call(args)

    assert "error" in result
    assert fake.calls == []


def test_the_tool_is_registered_with_a_schema():
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    assert "room_request_period" in builtin_handlers()
    schema = get_engine_schemas()["room_request_period"]["function"]
    assert schema["name"] == "room_request_period"
    params = schema["parameters"]
    assert set(params["required"]) == {"room_id", "grain", "anchor"}
    assert params["properties"]["grain"]["enum"] == ["day", "week", "month", "custom"]
