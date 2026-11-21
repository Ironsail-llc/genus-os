"""``POST /api/rooms/{id}/run`` and the ``room_run`` tool (stories B6, A2, B5).

Both are thin wrappers over ``robothor.rooms.runs.start_room_run``, which is
proven against Postgres in ``robothor/rooms/tests/test_runs.py``. Here: who the
caller is (the token, or for the bridge's SERVICE token the account named in
``requested_by`` with the role read from that account, never the body), and
each refusal's status: 202 started, 200 joined, 400 a service call naming
nobody, 403 no ``run`` (a viewer), 404 no access, 409 paused or archived, 422
a malformed period. Nothing is capped: there is no 429.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from robothor.auth.deps import AuthContext
from robothor.engine.tools.dispatch import ToolContext
from robothor.rooms import runs
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.models import Edition

ROOM = "7a0c0e8e-63c4-4c5b-9a43-0d6f6f0c2b11"
ALICE = "5b8e4a8a-0f6e-4e43-8f53-1f9d1d3c2a77"
BOB = "9c3f1d2e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
PATH = f"/api/rooms/{ROOM}/run"
SERVICE = AuthContext(
    user_id="genus-bridge", tenant_id="test-tenant", role="service", typ="service"
)


def _edition(preview: bool = False) -> Edition:
    return Edition(
        tenant_id="test-tenant",
        id="0d1c7a3e-8a4b-4f0e-9c55-6a3d7c1e2f90",
        room_id=ROOM,
        seq=3,
        kind="edition",
        status="building",
        preview=preview,
        period_start=date(2026, 11, 20),
        period_end=date(2026, 11, 20),
        grain="day",
        revision=1,
        requested_by=ALICE,
        created_at=datetime(2026, 11, 21, 9, 30, tzinfo=UTC),
    )


class FakeStart:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: Any = (_edition(), False)
        self.error: Exception | None = None

    async def __call__(self, actor: Any, room_id: str, **kw: Any) -> Any:
        self.calls.append({"actor": actor, "room_id": room_id, **kw})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake(monkeypatch) -> FakeStart:
    fake = FakeStart()
    monkeypatch.setattr(runs, "start_room_run", fake)
    return fake


@pytest.fixture
def accounts(monkeypatch) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {
        BOB: {"id": BOB, "tenant_id": "test-tenant", "role": "member", "status": "active"},
    }
    monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", lambda uid: rows.get(uid))
    return rows


def _app(auth: AuthContext | None = None) -> FastAPI:
    from robothor.engine import rooms_api

    app = FastAPI()
    caller = auth or AuthContext(user_id=ALICE, tenant_id="test-tenant", role="member", typ="user")

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = caller
        return await call_next(request)

    rooms_api.register(app)
    return app


async def _post(app: FastAPI, body: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(PATH, json=body)


class TestRoute:
    async def test_a_started_run_is_202_with_the_edition(self, fake):
        response = await _post(_app(), {})

        assert response.status_code == 202
        body = response.json()
        assert body["id"] == _edition().id and body["status"] == "building"
        assert body["periodStart"] == "2026-11-20"
        call = fake.calls[0]
        assert call["actor"].user_id == ALICE and call["actor"].role == "member"
        assert call["room_id"] == ROOM
        assert call["preview"] is False and call["period"] is None

    async def test_a_joined_run_is_200(self, fake):
        fake.result = (_edition(), True)
        assert (await _post(_app(), {})).status_code == 200

    async def test_preview_and_period_are_passed_on(self, fake):
        period = {"grain": "week", "start": "2026-11-09", "end": "2026-11-15"}
        await _post(_app(), {"preview": True, "period": period})
        assert fake.calls[0]["preview"] is True
        assert fake.calls[0]["period"] == period

    async def test_a_human_tokens_requested_by_is_ignored(self, fake, accounts):
        await _post(_app(), {"requested_by": BOB})
        assert fake.calls[0]["actor"].user_id == ALICE

    async def test_the_bridge_names_the_person_and_their_role_is_read(self, fake, accounts):
        response = await _post(_app(SERVICE), {"requested_by": BOB, "role": "owner"})

        assert response.status_code == 202
        actor = fake.calls[0]["actor"]
        assert (actor.user_id, actor.role, actor.is_service) == (BOB, "member", False)

    async def test_the_bridge_must_name_someone(self, fake, accounts):
        response = await _post(_app(SERVICE), {})
        assert response.status_code == 400
        assert fake.calls == []

    @pytest.mark.parametrize("change", ["missing", "inactive", "other-tenant"])
    async def test_an_account_that_cannot_stand_here_is_404(self, fake, accounts, change):
        if change == "missing":
            accounts.pop(BOB)
        elif change == "inactive":
            accounts[BOB]["status"] = "disabled"
        else:
            accounts[BOB]["tenant_id"] = "test-tenant-other"

        response = await _post(_app(SERVICE), {"requested_by": BOB})

        assert response.status_code == 404
        assert fake.calls == []

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (RoomNotFound(ROOM), 404),
            (RoomForbidden("viewer access does not allow 'run'"), 403),
            (runs.RunRefused("paused", "the workspace is paused"), 409),
            (runs.RunRefused("archived", "the workspace is archived"), 409),
            (ValueError("period must be {grain, start, end}"), 422),
        ],
    )
    async def test_each_refusal_has_its_status(self, fake, error, status):
        fake.error = error
        response = await _post(_app(), {})
        assert response.status_code == status
        assert "error" in response.json()

    async def test_only_the_bridge_may_act_for_a_person(self, fake, accounts):
        other = AuthContext(
            user_id="svc-other", tenant_id="test-tenant", role="service", typ="service"
        )
        response = await _post(_app(other), {"requested_by": BOB})
        assert response.status_code == 403
        assert fake.calls == []

    @pytest.mark.parametrize(
        "error",
        [
            RoomForbidden("SECRET-internal detail"),
            runs.RunRefused("paused", "SECRET-internal detail"),
            ValueError("SECRET-internal detail"),
        ],
    )
    async def test_a_refusal_never_carries_the_exceptions_text(self, fake, error):
        fake.error = error
        response = await _post(_app(), {})
        assert "SECRET" not in response.text

    async def test_an_unexpected_error_is_a_generic_500(self, fake):
        fake.error = RuntimeError("SECRET-internal detail")
        response = await _post(_app(), {})
        assert response.status_code == 500
        assert response.json()["error"] == "internal_error"
        assert "SECRET" not in response.text

    async def test_a_non_boolean_preview_is_422(self, fake):
        response = await _post(_app(), {"preview": "yes please"})
        assert response.status_code == 422
        assert fake.calls == []

    async def test_the_account_lookup_runs_off_the_loop(self, fake, monkeypatch):
        import time

        def slow(uid: str) -> dict[str, Any]:
            time.sleep(0.2)
            return {"id": uid, "tenant_id": "test-tenant", "role": "member", "status": "active"}

        monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", slow)
        loop = asyncio.get_running_loop()
        ticks: list[float] = []

        async def probe() -> None:
            for _ in range(10):
                ticks.append(loop.time())
                await asyncio.sleep(0.02)

        await asyncio.gather(_post(_app(SERVICE), {"requested_by": BOB}), probe())
        gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
        assert max(gaps) < 0.15


def _ctx(user: str = ALICE, role: str = "member") -> ToolContext:
    return ToolContext(tenant_id="test-tenant", user_id=user, user_role=role, agent_id="main")


class TestTool:
    async def _call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        from robothor.engine.tools.handlers.room_runs import HANDLERS

        return await HANDLERS["room_run"](args, ctx)

    async def test_it_starts_a_run_for_the_person(self, fake):
        result = await self._call({"room_id": ROOM, "preview": True}, _ctx())

        assert result["started"] is True and result["joined"] is False
        assert result["link"] == f"/?v=chat&room={ROOM}"
        assert result["edition"]["id"] == _edition().id
        call = fake.calls[0]
        assert call["actor"].user_id == ALICE and call["preview"] is True

    async def test_a_joined_run_says_so(self, fake):
        fake.result = (_edition(), True)
        result = await self._call({"room_id": ROOM}, _ctx())
        assert result["joined"] is True and result["started"] is False

    async def test_a_run_with_no_person_is_refused(self, fake):
        result = await self._call({"room_id": ROOM}, _ctx(user="service:main", role="service"))
        assert "error" in result and fake.calls == []

    @pytest.mark.parametrize(
        "error",
        [
            RoomNotFound(ROOM),
            RoomForbidden("viewer access does not allow 'run'"),
            runs.RunRefused("paused", "the workspace is paused"),
            ValueError("bad period"),
        ],
    )
    async def test_refusals_are_errors_the_agent_can_relay(self, fake, error):
        fake.error = error
        result = await self._call({"room_id": ROOM}, _ctx())
        assert "error" in result
        assert ROOM not in result["error"]

    def test_nothing_in_the_run_path_is_capped(self):
        from robothor.engine import rooms_api

        assert not hasattr(runs, "check_on_demand_cap")
        assert not hasattr(runs, "OnDemandCapReached")
        assert not hasattr(runs, "DEFAULT_MAX_COST_USD")
        assert "on_demand_cap" not in open(rooms_api.__file__).read().split("def _run_answer")[1]

    def test_the_tool_is_a_workspace_tool_with_a_schema(self):
        from robothor.engine.tools.constants import ROOM_TOOLS
        from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS
        from robothor.engine.tools.schemas import get_engine_schemas

        assert "room_run" in ROOM_TOOLS
        assert "room_run" in _BENCHMARK_EXCLUDED_TOOLS
        schema = get_engine_schemas()["room_run"]["function"]
        assert schema["parameters"]["required"] == ["room_id"]
        assert set(schema["parameters"]["properties"]) == {"room_id", "preview", "period"}
