"""Chat on a room's session: access, attribution, one turn at a time.

A room ("Workspace" in the UI) is a company-owned conversation several people
share. The dashboard names it by ``room_id``; the engine checks access with
``robothor.rooms.access`` and resolves the room's own session key, which a
browser never sends or sees. Stories:

- D1 continue the conversation: the agent gets the room's bounded history,
  including turns written by a path other than chat, loaded under the ROOM's
  tenant.
- D2 attribution: each user turn records who said it; history items carry
  ``authorUserId`` / ``authorName`` / ``editionId``; the model sees ``[Name]``.
- D3 two people at once: one turn per room; a second sender gets 409
  ``room_busy`` naming who is being answered; the lock is released on error
  and on cancel.
- D5 a viewer reads and cannot write.
- F3 the main conversation is unchanged: every non-room key resolves exactly as
  before, for owner, member and service token, in every per-user-sessions mode.

The data layer is faked here (``dal.get_room`` / ``get_member``) so the real
access rules run; ``test_chat_rooms_pg.py`` drives the same routes against
Postgres.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.chat import _effective_session_key, _sessions, init_chat, router
from robothor.engine.chat_steer import router as steer_router
from robothor.engine.chat_steer import webchat_run_key
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.rooms.access import room_session_key
from robothor.rooms.models import Room, RoomMember

TENANT = "test-tenant"
OTHER_TENANT = "test-tenant-other"
ALICE, BOB, CAROL, DAVE = (str(uuid.uuid4()) for _ in range(4))
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol", DAVE: "Dave"}
MODES = ("off", "observe", "enforce")


def _auth(user: str, role: str = "member", tenant: str = TENANT, **kw: Any) -> AuthContext:
    return AuthContext(user_id=user, tenant_id=tenant, role=role, typ="user", **kw)


def _service() -> AuthContext:
    return AuthContext(
        user_id="svc-agent", tenant_id=TENANT, role="service", typ="service", agent_id="main"
    )


def _run(text: str = "ok") -> AgentRun:
    return AgentRun(status=RunStatus.COMPLETED, output_text=text, trigger_type=TriggerType.WEBCHAT)


class FakeRooms:
    """Rooms and memberships the access rules read, plus the store and the bus."""

    def __init__(self) -> None:
        self.rooms: dict[tuple[str, str], Room] = {}
        self.members: dict[tuple[str, str, str], RoomMember] = {}
        self.store: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.activity = MagicMock(name="record_activity")
        self.published: list[tuple[Any, ...]] = []
        self.loads: list[dict[str, Any]] = []
        self._next_id = 100

    def add(self, *, tenant: str = TENANT, agent: str = "main", archived: bool = False) -> Room:
        from datetime import UTC, datetime

        room_id = str(uuid.uuid4())
        room = Room(
            tenant_id=tenant,
            id=room_id,
            kind="agent",
            title="Pricing",
            agent_id=agent,
            session_key=room_session_key(agent, room_id),
            created_by=ALICE,
            archived_at=datetime.now(UTC) if archived else None,
        )
        self.rooms[(tenant, room_id)] = room
        return room

    def share(self, room: Room, user: str, *, assigned: bool = False) -> None:
        self.members[(room.tenant_id, room.id, user)] = RoomMember(
            tenant_id=room.tenant_id, room_id=room.id, user_id=user, assigned=assigned
        )

    # -- the fakes --------------------------------------------------------

    def get_room(self, tenant_id: str, room_id: str) -> Room | None:
        return self.rooms.get((tenant_id, room_id))

    def get_member(self, tenant_id: str, room_id: str, user_id: str) -> RoomMember | None:
        return self.members.get((tenant_id, room_id, user_id))

    def load_session(self, key: str, limit: int = 20, tenant_id: str = "") -> dict[str, Any]:
        self.loads.append({"key": key, "limit": limit, "tenant_id": tenant_id})
        rows = self.store.get((tenant_id, key))
        return {"history": list(rows[-limit:]), "model_override": None} if rows else {}

    def load_history_page(self, key: str, *, limit: int, tenant_id: str = "", **_: Any) -> Any:
        rows = self.store.get((tenant_id, key))
        if not rows:
            return None
        page = [
            {**row, "id": i + 1, "timestamp": "2026-11-21T09:00:00+00:00"}
            for i, row in enumerate(rows)
        ][-limit:]
        return {"messages": page, "hasMore": False}

    async def save_exchange(self, key: str, user: str, reply: str, **kw: Any) -> list[int]:
        extras = kw.get("user_extras") or {}
        rows = self.store.setdefault((kw.get("tenant_id", ""), key), [])
        rows.append({"role": "user", "content": user, **extras})
        rows.append({"role": "assistant", "content": reply})
        self._next_id += 2
        return [self._next_id - 1, self._next_id]

    def publish(self, *args: Any, **kwargs: Any) -> str:
        self.published.append((args, kwargs))
        return "1-0"


@pytest.fixture
def fake(monkeypatch) -> FakeRooms:
    fake = FakeRooms()
    monkeypatch.setattr("robothor.rooms.dal.get_room", fake.get_room)
    monkeypatch.setattr("robothor.rooms.dal.get_member", fake.get_member)
    monkeypatch.setattr("robothor.rooms.dal.record_activity", fake.activity, raising=False)
    monkeypatch.setattr("robothor.engine.chat_store.load_session", fake.load_session)
    monkeypatch.setattr("robothor.engine.chat_store.load_history_page", fake.load_history_page)
    monkeypatch.setattr("robothor.engine.chat.save_exchange_async", fake.save_exchange)
    monkeypatch.setattr("robothor.events.bus.publish", fake.publish)
    monkeypatch.setattr(
        "robothor.engine.chat._resolve_webchat_identity",
        lambda auth: (
            None if auth.is_service else SimpleNamespace(display_name=NAMES.get(auth.user_id, ""))
        ),
    )
    return fake


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    runner.execute = AsyncMock(return_value=_run("reply"))
    return runner


@pytest.fixture
async def client(engine_config, mock_runner, isolated_plan_claims, fake):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    app.include_router(steer_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    _sessions.clear()


def _as(auth: AuthContext) -> Any:
    return patch("robothor.engine.chat._auth_context", return_value=auth)


async def _send(client: AsyncClient, auth: AuthContext, **body: Any) -> Any:
    with _as(auth):
        return await client.post("/chat/send", json={"message": "hello", **body})


def _detail(runner: MagicMock) -> str:
    return runner.execute.await_args.kwargs["trigger_detail"]


# ─── access: who reaches a room's session ─────────────────────────────


class TestAccess:
    @pytest.mark.parametrize("mode", MODES)
    async def test_a_shared_member_sends_into_the_rooms_session(
        self, client, fake, mock_runner, monkeypatch, mode
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        room = fake.add()
        fake.share(room, BOB)

        res = await _send(client, _auth(BOB), room_id=room.id)

        assert res.status_code == 200
        assert _detail(mock_runner) == f"webchat:{room.session_key}"
        assert mock_runner.execute.await_args.kwargs["agent_id"] == "main"

    @pytest.mark.parametrize("mode", MODES)
    async def test_the_tenant_owner_reaches_any_room_not_the_main_session(
        self, client, fake, mock_runner, monkeypatch, mode
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        room = fake.add()

        res = await _send(client, _auth(ALICE, role="owner"), room_id=room.id)

        assert res.status_code == 200
        assert _detail(mock_runner) == f"webchat:{room.session_key}"

    @pytest.mark.parametrize("mode", MODES)
    async def test_a_non_member_gets_404_in_every_mode(
        self, client, fake, mock_runner, monkeypatch, mode
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        room = fake.add()

        sent = await _send(client, _auth(CAROL), room_id=room.id)
        with _as(_auth(CAROL)):
            read = await client.get(f"/chat/history?room_id={room.id}")

        assert sent.status_code == 404
        assert read.status_code == 404
        assert "error" in sent.json()
        mock_runner.execute.assert_not_awaited()

    @pytest.mark.parametrize("mode", MODES)
    async def test_a_raw_room_key_is_authorised_like_a_room_id(
        self, client, fake, mock_runner, monkeypatch, mode
    ):
        """``off`` and ``observe`` used to hand any key back verbatim."""
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        room = fake.add()
        fake.share(room, BOB)

        refused = await _send(client, _auth(CAROL), session_key=room.session_key)
        allowed = await _send(client, _auth(BOB), session_key=room.session_key)

        assert refused.status_code == 404
        assert allowed.status_code == 200
        assert _detail(mock_runner) == f"webchat:{room.session_key}"

    async def test_a_member_of_the_same_room_id_in_another_tenant_gets_404(
        self, client, fake, mock_runner
    ):
        room = fake.add()
        fake.share(room, BOB)

        res = await _send(client, _auth(BOB, tenant=OTHER_TENANT), room_id=room.id)

        assert res.status_code == 404
        mock_runner.execute.assert_not_awaited()

    async def test_an_acting_admin_of_another_tenant_gets_404(self, client, fake, mock_runner):
        room = fake.add()

        res = await _send(client, _auth(ALICE, role="owner", tenant=OTHER_TENANT), room_id=room.id)

        assert res.status_code == 404

    @pytest.mark.parametrize("room_id", ["not-a-uuid", "", "1234", "../etc"])
    async def test_a_malformed_room_id_is_404_or_ignored(
        self, client, fake, mock_runner, room_id, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
        res = await _send(client, _auth(BOB), room_id=room_id)
        if room_id:
            assert res.status_code == 404
            mock_runner.execute.assert_not_awaited()
        else:  # no room named at all: the caller's own conversation
            assert res.status_code == 200
            assert "room" not in _detail(mock_runner)

    @pytest.mark.parametrize("mode", MODES)
    async def test_a_room_key_naming_another_agent_is_404_even_for_the_owner(
        self, client, fake, mock_runner, monkeypatch, mode
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        room = fake.add(agent="main")

        res = await _send(
            client, _auth(ALICE, role="owner"), session_key=room_session_key("research", room.id)
        )

        assert res.status_code == 404
        mock_runner.execute.assert_not_awaited()

    async def test_an_archived_room_reads_but_refuses_a_send(self, client, fake, mock_runner):
        room = fake.add(archived=True)
        fake.share(room, BOB)

        sent = await _send(client, _auth(BOB), room_id=room.id)
        with _as(_auth(BOB)):
            read = await client.get(f"/chat/history?room_id={room.id}")

        assert sent.status_code == 403
        assert read.status_code == 200
        mock_runner.execute.assert_not_awaited()

    async def test_naming_both_a_room_and_a_session_is_400(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)

        res = await _send(client, _auth(BOB), room_id=room.id, session_key="agent:main:primary")
        with _as(_auth(BOB)):
            read = await client.get(
                f"/chat/history?room_id={room.id}&session_key=agent:main:primary"
            )

        assert res.status_code == 400
        assert read.status_code == 400
        mock_runner.execute.assert_not_awaited()

    async def test_a_service_token_resolves_a_room_id_without_membership(
        self, client, fake, mock_runner
    ):
        room = fake.add()

        res = await _send(client, _service(), room_id=room.id)

        assert res.status_code == 200
        assert _detail(mock_runner) == f"webchat:{room.session_key}"

    async def test_a_service_token_keeps_a_raw_room_key(self, client, fake, mock_runner):
        room = fake.add()

        res = await _send(client, _service(), session_key=room.session_key)

        assert res.status_code == 200
        assert _detail(mock_runner) == f"webchat:{room.session_key}"

    async def test_a_service_token_gets_404_for_a_room_of_another_tenant(
        self, client, fake, mock_runner
    ):
        room = fake.add(tenant=OTHER_TENANT)

        res = await _send(client, _service(), room_id=room.id)

        assert res.status_code == 404

    @pytest.mark.parametrize("mode", MODES)
    def test_the_sync_resolver_never_hands_a_human_a_room_key(self, monkeypatch, mode):
        """A route that skipped the room check must fail closed, not open."""
        from fastapi import HTTPException

        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        key = room_session_key("main", str(uuid.uuid4()))
        for auth in (_auth(BOB), _auth(ALICE, role="owner")):
            with pytest.raises(HTTPException) as refused:
                _effective_session_key(auth, key)
            assert refused.value.status_code == 404
        assert _effective_session_key(_service(), key) == key


# ─── D5: a viewer reads, and every mutating route refuses ─────────────


_MUTATING = [
    ("/chat/send", {"message": "hi"}),
    ("/chat/inject", {"message": "note"}),
    ("/chat/abort", {}),
    ("/chat/clear", {}),
    ("/chat/plan/start", {"message": "plan it"}),
    ("/chat/plan/approve", {"plan_id": "p1"}),
    ("/chat/plan/reject", {"plan_id": "p1"}),
    ("/chat/plan/iterate", {"plan_id": "p1", "feedback": "shorter"}),
    ("/chat/deep/start", {"query": "why"}),
    ("/chat/steer", {"text": "faster", "request_id": str(uuid.uuid4())}),
    ("/chat/interrupt", {"request_id": str(uuid.uuid4())}),
]

_READS = [
    "/chat/history",
    "/chat/export",
    "/chat/plan/status",
    "/chat/deep/status",
]


class TestViewer:
    @pytest.mark.parametrize(("path", "body"), _MUTATING)
    async def test_a_viewer_is_refused_every_mutating_route(
        self, client, fake, mock_runner, path, body
    ):
        room = fake.add()
        fake.share(room, DAVE)

        with _as(_auth(DAVE, role="viewer")):
            res = await client.post(path, json={**body, "room_id": room.id})

        assert res.status_code == 403
        assert "error" in res.json()
        mock_runner.execute.assert_not_awaited()

    @pytest.mark.parametrize("path", _READS)
    async def test_a_viewer_reads(self, client, fake, path):
        room = fake.add()
        fake.share(room, DAVE)

        with _as(_auth(DAVE, role="viewer")):
            res = await client.get(f"{path}?room_id={room.id}")

        assert res.status_code == 200

    @pytest.mark.parametrize(("path", "body"), _MUTATING)
    async def test_a_stranger_gets_404_on_every_mutating_route(
        self, client, fake, mock_runner, path, body
    ):
        room = fake.add()

        with _as(_auth(CAROL)):
            res = await client.post(path, json={**body, "room_id": room.id})

        assert res.status_code == 404

    @pytest.mark.parametrize("path", [*_READS, "/chat/outcome"])
    async def test_a_stranger_gets_404_on_every_read(self, client, fake, path):
        room = fake.add()

        with _as(_auth(CAROL)):
            res = await client.get(f"{path}?room_id={room.id}&request_id={uuid.uuid4()}")

        assert res.status_code == 404

    async def test_a_shared_member_may_not_clear_or_inject_into_a_shared_room(self, client, fake):
        """Clearing wipes everyone's history; an injected system row steers everyone's
        turns. Both are for the people who manage the room."""
        room = fake.add()
        fake.share(room, BOB)
        fake.share(room, CAROL, assigned=True)

        with _as(_auth(BOB)):
            cleared = await client.post("/chat/clear", json={"room_id": room.id})
            injected = await client.post("/chat/inject", json={"room_id": room.id, "message": "x"})
        with _as(_auth(CAROL)):
            managed = await client.post("/chat/inject", json={"room_id": room.id, "message": "x"})

        assert cleared.status_code == 403
        assert injected.status_code == 403
        assert managed.status_code == 200


class TestRoomIdOnEveryRoute:
    async def test_steer_and_interrupt_name_the_turn_in_the_rooms_session(
        self, client, fake, monkeypatch
    ):
        room = fake.add()
        fake.share(room, BOB)
        steer = MagicMock(return_value=True)
        interrupt = MagicMock(return_value=True)
        monkeypatch.setattr("robothor.engine.live_control.steer_conversation", steer)
        monkeypatch.setattr("robothor.engine.live_control.interrupt_conversation", interrupt)
        request_id = str(uuid.uuid4())
        expected = webchat_run_key(_auth(BOB), room.session_key, request_id)

        with _as(_auth(BOB)):
            s = await client.post(
                "/chat/steer", json={"room_id": room.id, "text": "go", "request_id": request_id}
            )
            i = await client.post(
                "/chat/interrupt", json={"room_id": room.id, "request_id": request_id}
            )

        assert s.status_code == i.status_code == 200
        assert steer.call_args.args[0] == expected
        assert interrupt.call_args.args[0] == expected

    async def test_outcome_reads_the_rooms_session(self, client, fake, monkeypatch):
        room = fake.add()
        fake.share(room, BOB)
        seen: list[str] = []

        def read_outcome(auth: Any, key: str, request_id: str) -> dict[str, Any]:
            seen.append(key)
            return {"terminal": False}

        monkeypatch.setattr("robothor.engine.chat_recovery.read_outcome", read_outcome)
        with _as(_auth(BOB)):
            res = await client.get(f"/chat/outcome?room_id={room.id}&request_id={uuid.uuid4()}")

        assert res.status_code == 200
        assert seen == [room.session_key]

    async def test_abort_in_a_room_reaches_the_rooms_session(self, client, fake):
        room = fake.add()
        fake.share(room, BOB)

        with _as(_auth(BOB)), patch("robothor.engine.runtime.controls.issue_request", MagicMock()):
            res = await client.post("/chat/abort", json={"room_id": room.id})

        assert res.status_code == 200
        assert room.session_key in _sessions


# ─── D1 + D2: history, attribution, reload ────────────────────────────


class TestAttribution:
    async def test_a_room_turn_records_its_author(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)

        await _send(client, _auth(BOB), room_id=room.id, message="price it at 40")

        user_row = fake.store[(TENANT, room.session_key)][0]
        assert user_row["author_user_id"] == BOB
        assert user_row["author_display_name"] == "Bob"
        assert user_row["content"] == "price it at 40"

    async def test_the_model_sees_each_speaker_by_name(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)
        fake.share(room, CAROL)
        await _send(client, _auth(BOB), room_id=room.id, message="price it at 40")

        await _send(client, _auth(CAROL), room_id=room.id, message="no, 45")

        kwargs = mock_runner.execute.await_args.kwargs
        assert kwargs["message"] == "[Carol] no, 45"
        history = kwargs["conversation_history"]
        assert history[0]["content"] == "[Bob] price it at 40"
        assert history[1] == {"role": "assistant", "content": "reply"}

    async def test_history_items_carry_the_author_and_never_the_session_key(
        self, client, fake, mock_runner
    ):
        room = fake.add()
        fake.share(room, BOB)
        fake.share(room, CAROL)
        fake.store[(TENANT, room.session_key)] = [
            {"role": "assistant", "content": "Week 46 is out", "edition_id": "e-1"},
        ]
        await _send(client, _auth(BOB), room_id=room.id, message="why the dip?")
        _sessions.clear()  # a reload: nothing in RAM

        with _as(_auth(CAROL)):
            res = await client.get(f"/chat/history?room_id={room.id}")

        body = res.json()
        assert res.status_code == 200
        assert "sessionKey" not in body
        assert room.session_key not in res.text
        assert body["roomId"] == room.id
        edition, question, _answer = body["messages"]
        assert edition["editionId"] == "e-1"
        assert question["authorUserId"] == BOB
        assert question["authorName"] == "Bob"

    async def test_the_agent_continues_with_history_written_outside_chat(
        self, client, fake, mock_runner
    ):
        """D1: a published edition's message is in the store, not in RAM."""
        room = fake.add()
        fake.share(room, BOB)
        await _send(client, _auth(BOB), room_id=room.id, message="first")
        fake.store[(TENANT, room.session_key)].append(
            {"role": "assistant", "content": "Week 46 is out", "edition_id": "e-1"}
        )

        await _send(client, _auth(BOB), room_id=room.id, message="why the dip?")

        history = mock_runner.execute.await_args.kwargs["conversation_history"]
        assert history[-1]["content"] == "Week 46 is out"

    async def test_the_agents_history_is_bounded(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)
        fake.store[(TENANT, room.session_key)] = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(3 * chat.MAX_HISTORY)
        ]

        await _send(client, _auth(BOB), room_id=room.id)

        history = mock_runner.execute.await_args.kwargs["conversation_history"]
        assert 0 < len(history) <= chat.MAX_HISTORY
        assert history[-1]["content"] == f"m{3 * chat.MAX_HISTORY - 1}"

    async def test_a_room_loads_under_the_rooms_tenant_for_an_acting_caller(
        self, client, fake, mock_runner
    ):
        """The engine's own tenant is not the room's: an acting admin of a child
        tenant must get the room's history, not an empty one."""
        child = "test-tenant-child"
        room = fake.add(tenant=child)
        fake.store[(child, room.session_key)] = [{"role": "assistant", "content": "earlier"}]
        acting = _auth(ALICE, role="admin", tenant=child, home_tenant_id=TENANT)

        res = await _send(client, acting, room_id=room.id)

        assert res.status_code == 200
        assert {"key": room.session_key, "limit": chat.MAX_HISTORY, "tenant_id": child} in (
            fake.loads
        )
        history = mock_runner.execute.await_args.kwargs["conversation_history"]
        assert history == [{"role": "assistant", "content": "earlier"}]

    async def test_a_saved_exchange_bumps_the_room_and_publishes_a_message_event(
        self, client, fake, mock_runner
    ):
        room = fake.add()
        fake.share(room, BOB)

        await _send(client, _auth(BOB), room_id=room.id)

        fake.activity.assert_called_once_with(TENANT, room.id, 102)
        (args, kwargs) = fake.published[-1]
        assert args[:2] == ("agent", "room.message")
        assert args[2] == {"room_id": room.id, "tenant_id": TENANT, "message_id": 102}
        assert kwargs["tenant_id"] == TENANT
        assert room.session_key not in repr(fake.published)


# ─── D3: one turn at a time ───────────────────────────────────────────


class TestOneTurnAtATime:
    async def test_a_second_sender_is_told_who_is_being_answered(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)
        fake.share(room, CAROL)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs: Any) -> AgentRun:
            entered.set()
            await release.wait()
            return _run("answer for Bob")

        mock_runner.execute = AsyncMock(side_effect=slow)
        first = asyncio.create_task(_send(client, _auth(BOB), room_id=room.id, message="one"))
        await asyncio.wait_for(entered.wait(), 2)

        busy = await _send(client, _auth(CAROL), room_id=room.id, message="two")

        assert busy.status_code == 409
        assert busy.json() == {"error": "room_busy", "answering": "Bob"}
        release.set()
        assert (await first).status_code == 200
        mock_runner.execute = AsyncMock(return_value=_run("answer for Carol"))
        again = await _send(client, _auth(CAROL), room_id=room.id, message="two")
        assert again.status_code == 200
        contents = [row["content"] for row in fake.store[(TENANT, room.session_key)]]
        assert contents == ["one", "answer for Bob", "two", "answer for Carol"]

    async def test_ordinary_sessions_never_wait_on_each_other(self, client, fake, mock_runner):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs: Any) -> AgentRun:
            entered.set()
            await release.wait()
            return _run()

        mock_runner.execute = AsyncMock(side_effect=slow)
        first = asyncio.create_task(_send(client, _auth(BOB)))
        await asyncio.wait_for(entered.wait(), 2)
        mock_runner.execute = AsyncMock(return_value=_run())

        second = await _send(client, _auth(CAROL))

        assert second.status_code == 200
        release.set()
        await first

    async def test_the_lock_is_released_when_the_turn_fails(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)
        mock_runner.execute = AsyncMock(side_effect=RuntimeError("provider down"))

        failed = await _send(client, _auth(BOB), room_id=room.id)
        mock_runner.execute = AsyncMock(return_value=_run())
        after = await _send(client, _auth(BOB), room_id=room.id)

        assert failed.status_code == 200  # the error rides the stream
        assert "provider down" in failed.text
        assert after.status_code == 200
        assert "room_busy" not in after.text

    async def test_the_lock_is_released_when_the_turn_is_cancelled(self, client, fake, mock_runner):
        room = fake.add()
        fake.share(room, BOB)
        entered = asyncio.Event()

        async def forever(**kwargs: Any) -> AgentRun:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        mock_runner.execute = AsyncMock(side_effect=forever)
        first = asyncio.create_task(_send(client, _auth(BOB), room_id=room.id))
        await asyncio.wait_for(entered.wait(), 2)
        with _as(_auth(BOB)), patch("robothor.engine.runtime.controls.issue_request", MagicMock()):
            stopped = await client.post("/chat/abort", json={"room_id": room.id})
        await asyncio.wait_for(first, 2)
        mock_runner.execute = AsyncMock(return_value=_run())

        after = await _send(client, _auth(BOB), room_id=room.id)

        assert stopped.json()["aborted"] is True
        assert after.status_code == 200


# ─── F3: the main conversation is unchanged ───────────────────────────


_MATRIX = [
    # (who, mode, requested, expected key)
    *[("owner", m, "agent:main:primary", "agent:main:primary") for m in MODES],
    *[("service", m, "agent:main:primary", "agent:main:primary") for m in MODES],
    ("member", "off", "agent:main:primary", "agent:main:primary"),
    ("member", "observe", "agent:main:primary", "agent:main:primary"),
    ("member", "enforce", "agent:main:primary", f"agent:main:user:{BOB}"),
    ("member", "enforce", "", f"agent:main:user:{BOB}"),
    ("owner", "enforce", "", "agent:main:primary"),
]


class TestMainConversationUnchanged:
    @pytest.mark.parametrize(("who", "mode", "requested", "expected"), _MATRIX)
    async def test_non_room_keys_resolve_as_before(
        self, client, fake, mock_runner, monkeypatch, who, mode, requested, expected
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", mode)
        auth = {"owner": _auth(ALICE, role="owner"), "member": _auth(BOB), "service": _service()}[
            who
        ]

        res = await _send(client, auth, session_key=requested)
        with _as(auth):
            read = await client.get(f"/chat/history?session_key={requested}")

        assert res.status_code == 200
        assert _detail(mock_runner) == f"webchat:{expected}"
        assert read.json()["sessionKey"] == expected

    async def test_the_shared_owner_session_carries_no_attribution(self, client, fake, mock_runner):
        await _send(client, _auth(ALICE, role="owner"), message="morning")

        kwargs = mock_runner.execute.await_args.kwargs
        assert kwargs["message"] == "morning"
        stored = fake.store[(TENANT, "agent:main:primary")]
        assert stored[0] == {"role": "user", "content": "morning"}
        fake.activity.assert_not_called()
        assert fake.published == []
        with _as(_auth(ALICE, role="owner")):
            items = (await client.get("/chat/history")).json()["messages"]
        assert all("authorUserId" not in item and "authorName" not in item for item in items)

    async def test_a_channel_surface_in_the_main_session_is_not_relabelled(self, client, fake):
        """``author_display_name`` already exists on channel-bus rows of the main
        session; the room mapping must not leak onto them."""
        fake.store[(TENANT, "agent:main:primary")] = [
            {
                "role": "assistant",
                "content": "done",
                "author_agent_id": "devops",
                "author_display_name": "DevOps",
            },
        ]
        with _as(_auth(ALICE, role="owner")):
            items = (await client.get("/chat/history")).json()["messages"]

        assert "authorName" not in items[0]


# ─── no blocking call on the loop ─────────────────────────────────────


async def test_the_room_lookup_runs_off_the_loop(client, fake, monkeypatch):
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor

    await warm_executor()
    room = fake.add()
    fake.share(room, BOB)
    probe = SlowProbe(asyncio.get_running_loop(), result=room)
    monkeypatch.setattr("robothor.rooms.dal.get_room", probe)

    with _as(_auth(BOB)):
        res = await client.get(f"/chat/history?room_id={room.id}")

    assert res.status_code == 200
    await probe.settle()
    probe.assert_loop_free()


async def test_the_activity_bump_runs_off_the_loop(client, fake, monkeypatch, mock_runner):
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor

    await warm_executor()
    room = fake.add()
    fake.share(room, BOB)
    probe = SlowProbe(asyncio.get_running_loop(), result=room)
    monkeypatch.setattr("robothor.rooms.dal.record_activity", probe, raising=False)

    res = await _send(client, _auth(BOB), room_id=room.id)

    assert res.status_code == 200
    await probe.settle()
    probe.assert_loop_free()
