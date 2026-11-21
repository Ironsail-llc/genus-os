# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""Interactive chat on a workspace, where it meets the workspace's runs.

- An interactive workspace turn runs with the room bound
  (``room_run_scope(RoomRun.for_session(...))``) around the runner AND the
  save of its reply, so ``room_publish_edition`` works there and the saved
  reply carries the edition; a main-conversation turn binds nothing, so the
  publish refuses it.
- An interrupted or cancelled workspace turn is saved by the interruption path
  with its author and moves the room's activity mark, like a finished one.
- Aborting needs ``post``; when what is running is the room's SCHEDULED run,
  only an assigned member or an admin may stop it.

Fixtures (fake rooms, a mock runner, the ASGI client) come from
``test_chat_rooms.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tests.test_chat_rooms import (  # noqa: F401 - fixtures by name
    ALICE,
    BOB,
    CAROL,
    TENANT,
    _as,
    _auth,
    _run,
    _send,
    client,
    fake,
    mock_runner,
)
from robothor.rooms import editions, runs
from robothor.rooms.run_context import current_room_run

if TYPE_CHECKING:
    from robothor.engine.models import AgentRun


class TestTheTurnIsBoundToItsRoom:
    async def test_a_workspace_turn_runs_and_saves_inside_the_rooms_scope(
        self, client, fake, mock_runner, monkeypatch
    ):
        room = fake.add()
        fake.share(room, BOB)
        seen: dict[str, Any] = {}

        async def execute(**kw: Any) -> AgentRun:
            seen["run"] = current_room_run()
            return _run("here is the board")

        mock_runner.execute = AsyncMock(side_effect=execute)
        original = fake.save_exchange

        async def save(*a: Any, **kw: Any) -> list[int]:
            seen["save"] = current_room_run()
            return await original(*a, **kw)

        monkeypatch.setattr("robothor.engine.chat.save_exchange_async", save)

        await _send(client, _auth(BOB), room_id=room.id)

        carrier = seen["run"]
        assert carrier is not None
        assert (carrier.tenant_id, carrier.room_id, carrier.session_key) == (
            TENANT,
            room.id,
            room.session_key,
        )
        assert carrier.preview is False and carrier.edition_id is None
        assert seen["save"] is carrier

    async def test_a_workspace_turn_can_publish_a_dashboard(self, client, fake, mock_runner):
        """The publish's own gate: the carrier it reads names this room."""
        room = fake.add()
        fake.share(room, BOB)
        results: list[dict[str, Any]] = []

        async def execute(**kw: Any) -> AgentRun:
            carrier = current_room_run()
            with patch.object(editions, "_publish", side_effect=_fake_publish(room)):
                results.append(
                    editions.publish_from_run(
                        {"room_id": room.id, "kind": "dashboard", "title": "Board"},
                        tenant_id=kw["tenant_id"],
                        agent_id=kw["agent_id"],
                        run_id="r-1",
                        user_id=kw["user_id"],
                        user_role=kw["user_role"],
                        run=carrier,
                    )
                )
            return _run("done")

        mock_runner.execute = AsyncMock(side_effect=execute)
        with patch.object(editions, "emit_edition_event", MagicMock()):
            await _send(client, _auth(BOB), room_id=room.id)

        assert results and results[0].get("published") is True

    async def test_a_main_conversation_turn_binds_no_room_and_cannot_publish(
        self, client, fake, mock_runner
    ):
        room = fake.add()
        results: list[dict[str, Any]] = []

        async def execute(**kw: Any) -> AgentRun:
            assert current_room_run() is None
            results.append(
                editions.publish_from_run(
                    {"room_id": room.id, "kind": "dashboard", "title": "Board"},
                    tenant_id=kw["tenant_id"],
                    agent_id=kw["agent_id"],
                    run_id="r-1",
                    user_id=kw["user_id"],
                    user_role=kw["user_role"],
                    run=current_room_run(),
                )
            )
            return _run("done")

        mock_runner.execute = AsyncMock(side_effect=execute)

        await _send(client, _auth(ALICE, role="owner"))

        assert "error" in results[0]


def _fake_publish(room: Any) -> Any:
    """``editions._publish`` past its gate, without a database."""
    from robothor.rooms.models import Edition

    real_gate = editions._room_for_run

    def publish(args: Any, actor: Any, agent_id: str, run_id: str, run: Any) -> Any:
        real_gate(args, actor, agent_id, run)  # raises unless the carrier names this room
        edition = Edition(
            tenant_id=room.tenant_id,
            id="e-1",
            room_id=room.id,
            seq=1,
            kind="dashboard",
            status="published",
            preview=False,
            period_start=None,
            period_end=None,
            grain=None,
            revision=1,
        )
        return room, edition

    return publish


class TestAnInterruptedTurn:
    async def test_it_is_saved_with_its_author_and_bumps_the_room(
        self, client, fake, mock_runner, monkeypatch
    ):
        from robothor.engine.runtime.current import active_context

        room = fake.add()
        fake.share(room, BOB)
        entered = asyncio.Event()

        async def forever(**kw: Any) -> AgentRun:
            active_context.set(
                SimpleNamespace(tenant_id=TENANT, principal_id=BOB, request_id="req-1")
            )
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        mock_runner.execute = AsyncMock(side_effect=forever)
        monkeypatch.setattr(
            "robothor.engine.chat_delivery._saved_request",
            lambda request_id, auth: {"run_id": "r-1", "text": "half an answer"},
        )
        monkeypatch.setattr("robothor.engine.chat_store.save_exchange_async", fake.save_exchange)

        first = asyncio.create_task(
            _send(client, _auth(BOB), room_id=room.id, message="how did we do?")
        )
        await asyncio.wait_for(entered.wait(), 2)
        with _as(_auth(BOB)), patch("robothor.engine.runtime.controls.issue_request", MagicMock()):
            await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.wait_for(first, 2)

        rows = fake.store[(TENANT, room.session_key)]
        assert rows[0]["content"] == "how did we do?"
        assert rows[0]["author_user_id"] == BOB
        assert rows[0]["author_display_name"] == "Bob"
        assert rows[1]["content"] == "half an answer"
        fake.activity.assert_called_once_with(TENANT, room.id, 102)
        (args, _kw) = fake.published[-1]
        assert args[1] == "room.message"


class TestAbortingARun:
    def _running(
        self, monkeypatch, room: Any, trigger: str, requested_by: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(asyncio.Event().wait())
        active = SimpleNamespace(
            room_id=room.id, trigger=trigger, task=task, requested_by=requested_by
        )
        monkeypatch.setattr(
            runs, "active_run", lambda room_id: active if room_id == room.id else None
        )
        return task

    async def test_a_shared_member_cannot_stop_a_scheduled_run(self, client, fake, monkeypatch):
        room = fake.add()
        fake.share(room, BOB)
        task = self._running(monkeypatch, room, "schedule")

        with _as(_auth(BOB)):
            res = await client.post("/chat/abort", json={"room_id": room.id})

        assert res.status_code == 403
        assert res.json() == {"error": "room_forbidden"}
        assert not task.cancelled()
        task.cancel()

    async def test_an_assigned_member_stops_a_scheduled_run(self, client, fake, monkeypatch):
        room = fake.add()
        fake.share(room, BOB, assigned=True)
        task = self._running(monkeypatch, room, "schedule")

        with _as(_auth(BOB)):
            res = await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.sleep(0)

        assert res.status_code == 200
        assert res.json() == {"aborted": True, "roomId": room.id}
        assert task.cancelled()

    async def test_an_admin_stops_a_scheduled_run(self, client, fake, monkeypatch):
        room = fake.add()
        task = self._running(monkeypatch, room, "schedule")

        with _as(_auth(ALICE, role="admin")):
            res = await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.sleep(0)

        assert res.status_code == 200 and task.cancelled()

    async def test_a_shared_member_stops_their_own_run_now(self, client, fake, monkeypatch):
        room = fake.add()
        fake.share(room, BOB)
        task = self._running(monkeypatch, room, "manual", requested_by=BOB)

        with _as(_auth(BOB)):
            res = await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.sleep(0)

        assert res.status_code == 200 and task.cancelled()

    @pytest.mark.parametrize("trigger", ["manual", "period", "preview"])
    async def test_a_shared_member_cannot_stop_someone_elses_run(
        self, client, fake, monkeypatch, trigger
    ):
        room = fake.add()
        fake.share(room, BOB)
        task = self._running(monkeypatch, room, trigger, requested_by=CAROL)

        with _as(_auth(BOB)):
            res = await client.post("/chat/abort", json={"room_id": room.id})

        assert res.status_code == 403
        assert res.json() == {"error": "room_forbidden"}
        assert not task.cancelled()
        task.cancel()

    @pytest.mark.parametrize("trigger", ["manual", "period", "preview"])
    async def test_an_assigned_member_stops_someone_elses_run(
        self, client, fake, monkeypatch, trigger
    ):
        room = fake.add()
        fake.share(room, BOB, assigned=True)
        task = self._running(monkeypatch, room, trigger, requested_by=CAROL)

        with _as(_auth(BOB)):
            res = await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.sleep(0)

        assert res.status_code == 200 and task.cancelled()

    async def test_an_admin_stops_someone_elses_preview(self, client, fake, monkeypatch):
        room = fake.add()
        task = self._running(monkeypatch, room, "preview", requested_by=CAROL)

        with _as(_auth(ALICE, role="admin")):
            res = await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.sleep(0)

        assert res.status_code == 200 and task.cancelled()

    async def test_a_viewer_stops_nothing(self, client, fake, monkeypatch):
        room = fake.add()
        fake.share(room, BOB)
        task = self._running(monkeypatch, room, "manual")

        with _as(_auth(BOB, role="viewer")):
            res = await client.post("/chat/abort", json={"room_id": room.id})

        assert res.status_code == 403 and not task.cancelled()
        task.cancel()
