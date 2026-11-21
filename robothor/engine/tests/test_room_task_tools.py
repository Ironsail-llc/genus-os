"""Task tools in workspaces: ``room_task_accept``, ``room_task_dismiss``, ``room_open_task``.

Thin wrappers over ``robothor.rooms.tasks`` (whose rules are proven against
real rows in ``robothor/rooms/tests/test_tasks.py``). What is proven here is
the tool surface: the caller is the human on the run, a refusal says why
without naming a workspace the caller cannot see, results carry the link,
the tools reach the main agent like the other workspace tools, viewers are
not granted them, and no call blocks the loop.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import room_tasks as task_tools
from robothor.rooms.models import Room

TASK_TOOLS = {"room_task_accept", "room_task_dismiss", "room_open_task"}
USER = str(uuid.uuid4())
ROOM_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())


def _ctx(**overrides: Any) -> ToolContext:
    fields: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": "test-tenant",
        "user_id": USER,
        "user_role": "member",
    }
    fields.update(overrides)
    return ToolContext(**fields)


def _call(tool: str, args: dict[str, Any], ctx: ToolContext | None = None) -> dict[str, Any]:
    return asyncio.run(task_tools.HANDLERS[tool](args, ctx or _ctx()))


def _view(**kw: Any):
    from robothor.rooms.tasks import RoomTaskView, TaskPerson

    base = {
        "task_id": TASK_ID,
        "title": "Follow up Acme",
        "status": "TODO",
        "origin": "suggestion",
        "edition_id": None,
        "assignee": TaskPerson(user_id=USER, display_name="Bob"),
        "accepted_by": TaskPerson(user_id=USER, display_name="Bob"),
        "dismissed": False,
    }
    base.update(kw)
    return RoomTaskView(**base)


def _room() -> Room:
    return Room(
        tenant_id="test-tenant",
        id=ROOM_ID,
        kind="task",
        title="Renew the contract",
        agent_id="main",
        session_key=f"agent:main:room:{ROOM_ID}",
        created_by=USER,
        task_id=TASK_ID,
    )


# ─── the surface ─────────────────────────────────────────────────────────


def test_the_module_registers_exactly_the_task_tools():
    assert set(task_tools.HANDLERS) == TASK_TOOLS


def test_every_task_tool_has_a_schema_is_dispatchable_and_is_a_workspace_tool():
    from robothor.engine.tools.constants import READONLY_TOOLS, ROOM_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    schemas = get_engine_schemas()
    assert set(schemas) >= TASK_TOOLS
    assert set(builtin_handlers()) >= TASK_TOOLS
    assert TASK_TOOLS <= ROOM_TOOLS
    assert not TASK_TOOLS & READONLY_TOOLS
    assert schemas["room_task_accept"]["function"]["parameters"]["required"] == [
        "room_id",
        "task_id",
    ]
    assert schemas["room_task_dismiss"]["function"]["parameters"]["required"] == [
        "room_id",
        "task_id",
    ]
    assert schemas["room_open_task"]["function"]["parameters"]["required"] == ["task_id"]


def test_the_main_agent_gets_them_and_another_agent_does_not():
    from robothor.engine.models import AgentConfig
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()
    assert set(registry.get_tool_names(AgentConfig(id="main", name="Main"))) >= TASK_TOOLS
    assert not TASK_TOOLS & set(registry.get_tool_names(AgentConfig(id="worker", name="W")))


# ─── results and refusals ────────────────────────────────────────────────


def test_accept_returns_the_task_who_has_it_and_the_link(monkeypatch):
    from robothor.rooms import tasks

    seen: list[Any] = []

    def accept(actor, room_id, task_id):
        seen.append((actor, room_id, task_id))
        return _view()

    monkeypatch.setattr(tasks, "accept_task", accept)

    result = _call("room_task_accept", {"room_id": ROOM_ID, "task_id": TASK_ID})

    [(actor, room_id, task_id)] = seen
    assert (actor.user_id, actor.role, actor.tenant_id) == (USER, "member", "test-tenant")
    assert (room_id, task_id) == (ROOM_ID, TASK_ID)
    assert result["task"]["task_id"] == TASK_ID
    assert result["task"]["assignee"] == {"user_id": USER, "display_name": "Bob"}
    assert result["link"] == f"/?v=chat&room={ROOM_ID}"


def test_a_second_accept_says_who_has_it(monkeypatch):
    from robothor.rooms import tasks
    from robothor.rooms.errors import TaskAlreadyAccepted
    from robothor.rooms.tasks import TaskPerson

    def accept(actor, room_id, task_id):
        raise TaskAlreadyAccepted(TaskPerson(user_id=str(uuid.uuid4()), display_name="Alice"))

    monkeypatch.setattr(tasks, "accept_task", accept)

    result = _call("room_task_accept", {"room_id": ROOM_ID, "task_id": TASK_ID})

    assert result["already_accepted"] is True
    assert "Alice" in result["error"]


@pytest.mark.parametrize("tool", ["room_task_accept", "room_task_dismiss"])
def test_not_found_names_nothing(monkeypatch, tool):
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomNotFound

    def missing(actor, room_id, task_id):
        raise RoomNotFound(room_id)

    monkeypatch.setattr(tasks, "accept_task", missing)
    monkeypatch.setattr(tasks, "dismiss_task", missing)

    result = _call(tool, {"room_id": ROOM_ID, "task_id": TASK_ID})

    assert result["not_found"] is True
    assert ROOM_ID not in result["error"]


def test_dismiss_reports_the_dismissal(monkeypatch):
    from robothor.rooms import tasks

    monkeypatch.setattr(
        tasks, "dismiss_task", lambda actor, room_id, task_id: _view(dismissed=True, assignee=None)
    )

    result = _call("room_task_dismiss", {"room_id": ROOM_ID, "task_id": TASK_ID})

    assert result["task"]["dismissed"] is True
    assert result["link"] == f"/?v=chat&room={ROOM_ID}"


def test_open_task_returns_the_workspace_and_its_link(monkeypatch):
    from robothor.rooms import tasks

    monkeypatch.setattr(tasks, "open_task_room", lambda actor, task_id: _room())

    result = _call("room_open_task", {"task_id": TASK_ID})

    assert result["room"]["id"] == ROOM_ID
    assert result["room"]["task_id"] == TASK_ID
    assert result["link"] == f"/?v=chat&room={ROOM_ID}"
    assert "session_key" not in result["room"]


def test_a_run_with_no_person_cannot_use_them(monkeypatch):
    from robothor.rooms import tasks

    called: list[str] = []
    monkeypatch.setattr(tasks, "open_task_room", lambda *a: called.append("open"))

    for tool, args in (
        ("room_open_task", {"task_id": TASK_ID}),
        ("room_task_accept", {"room_id": ROOM_ID, "task_id": TASK_ID}),
    ):
        result = _call(tool, args, _ctx(user_id="service:main", user_role="service"))
        assert "error" in result
    assert called == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("room_task_accept", {"task_id": TASK_ID}),
        ("room_task_dismiss", {"room_id": ROOM_ID}),
        ("room_open_task", {}),
    ],
)
def test_missing_arguments_are_refused(tool, args):
    assert "error" in _call(tool, args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "target", "args"),
    [
        ("room_task_accept", "accept_task", {"room_id": ROOM_ID, "task_id": TASK_ID}),
        ("room_task_dismiss", "dismiss_task", {"room_id": ROOM_ID, "task_id": TASK_ID}),
        ("room_open_task", "open_task_room", {"task_id": TASK_ID}),
    ],
)
async def test_every_task_tool_runs_its_database_work_off_the_loop(monkeypatch, tool, target, args):
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomNotFound

    await warm_executor()
    probe = SlowProbe(asyncio.get_running_loop())

    def slow(*a: Any, **k: Any) -> Any:
        probe(*a, **k)
        raise RoomNotFound("probe")

    monkeypatch.setattr(tasks, target, slow)

    result = await task_tools.HANDLERS[tool](args, _ctx())

    assert "error" in result
    await probe.settle()
    probe.assert_loop_free()
