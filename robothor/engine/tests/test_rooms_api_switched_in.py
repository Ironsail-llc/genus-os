"""Run now and periods for a parent-company admin switched into a company.

The bridge admits a parent company's owner or admin who has switched into a
child company (``POST /api/auth/switch-tenant``): their acting token names the
child, and their account lives at home in the parent. The bridge proxies Run
now and period requests with its SERVICE token, naming the person in
``requested_by`` and, when they act from another company, that home company in
``acting_from``. The engine admits exactly whom the bridge admits, by the same
rule the switch itself applies: the account is active, lives in ``acting_from``,
is an owner or admin there, and this engine's company is among that company's
descendants. It reads all of that itself and believes no role from the body;
when it cannot check, it refuses (404, as for anyone it does not know).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from robothor.auth.deps import AuthContext
from robothor.rooms import periods, runs
from robothor.rooms.models import Edition

ROOM = "7a0c0e8e-63c4-4c5b-9a43-0d6f6f0c2b11"
HAL = "4d4d4d4d-1111-4222-8333-000000000001"  # parent admin
PAT = "4d4d4d4d-1111-4222-8333-000000000002"  # parent member
SIB = "4d4d4d4d-1111-4222-8333-000000000003"  # a sibling company's admin
CHILD = "test-tenant"  # this engine's company
PARENT = "test-tenant-parent"
SIBLING = "test-tenant-sibling"

SERVICE = AuthContext(user_id="genus-bridge", tenant_id=CHILD, role="service", typ="service")


def _edition() -> Edition:
    return Edition(
        tenant_id=CHILD,
        id="0d1c7a3e-8a4b-4f0e-9c55-6a3d7c1e2f90",
        room_id=ROOM,
        seq=3,
        kind="edition",
        status="published",
        preview=False,
        period_start=date(2026, 11, 2),
        period_end=date(2026, 11, 8),
        grain="week",
        revision=1,
        requested_by=HAL,
        created_at=datetime(2026, 11, 21, 9, 30, tzinfo=UTC),
    )


@pytest.fixture
def world(monkeypatch) -> Any:
    rows: dict[str, dict[str, Any]] = {
        HAL: {"id": HAL, "tenant_id": PARENT, "role": "admin", "status": "active"},
        PAT: {"id": PAT, "tenant_id": PARENT, "role": "member", "status": "active"},
        SIB: {"id": SIB, "tenant_id": SIBLING, "role": "admin", "status": "active"},
    }
    tree = {PARENT: (PARENT, CHILD, SIBLING), SIBLING: (SIBLING,), CHILD: (CHILD,)}
    calls: list[tuple[str, Any, dict[str, Any]]] = []
    state: dict[str, Any] = {"hierarchy_error": None}

    def accessible(tenant_id: str, role: str | None = None, **kw: Any) -> tuple[str, ...]:
        if state["hierarchy_error"] is not None:
            raise state["hierarchy_error"]
        return tree.get(tenant_id, (tenant_id,)) if role in ("owner", "admin") else (tenant_id,)

    async def start(actor: Any, room_id: str, **kw: Any) -> Any:
        calls.append(("run", actor, kw))
        return _edition(), False

    async def request(actor: Any, room_id: str, grain: str, anchor: Any) -> Any:
        calls.append(("periods", actor, {"grain": grain, "anchor": anchor}))
        return _edition(), False  # the period's edition already exists

    monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", lambda uid: rows.get(uid))
    monkeypatch.setattr("robothor.engine.permissions.resolve_accessible_tenants", accessible)
    monkeypatch.setattr(runs, "start_room_run", start)
    monkeypatch.setattr(periods, "request_period", request)
    return type("World", (), {"rows": rows, "calls": calls, "state": state})


def _app() -> FastAPI:
    from robothor.engine import rooms_api

    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = SERVICE
        return await call_next(request)

    rooms_api.register(app)
    return app


ROUTES = {
    "run": (f"/api/rooms/{ROOM}/run", {}),
    "periods": (f"/api/rooms/{ROOM}/periods", {"grain": "week", "anchor": "2026-11-04"}),
}


async def _post(route: str, **body: Any) -> httpx.Response:
    path, base = ROUTES[route]
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json={**base, **body})


@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_a_switched_in_parent_admin_is_admitted_as_an_admin_here(world, route) -> None:
    answer = await _post(route, requested_by=HAL, acting_from=PARENT)

    assert answer.status_code == 200, answer.text
    [(called, actor, _kw)] = world.calls
    assert called == route
    assert (actor.user_id, actor.role, actor.tenant_id, actor.is_service) == (
        HAL,
        "admin",
        CHILD,
        False,
    )


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize(
    ("who", "acting_from"),
    [
        (HAL, None),  # the parent admin, not switched in
        (HAL, SIBLING),  # claims a home that is not theirs
        (SIB, SIBLING),  # a sibling company's admin
        (PAT, PARENT),  # an ordinary member of the parent
    ],
)
async def test_anyone_else_from_another_company_gets_404(world, route, who, acting_from) -> None:
    body: dict[str, Any] = {"requested_by": who}
    if acting_from is not None:
        body["acting_from"] = acting_from
    answer = await _post(route, **body)

    assert answer.status_code == 404
    assert answer.json()["error"] == "room_not_found"
    assert world.calls == []


@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_a_disabled_parent_admin_gets_404(world, route) -> None:
    world.rows[HAL]["status"] = "disabled"
    answer = await _post(route, requested_by=HAL, acting_from=PARENT)
    assert answer.status_code == 404
    assert world.calls == []


@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_when_the_hierarchy_cannot_be_read_it_refuses(world, route) -> None:
    world.state["hierarchy_error"] = RuntimeError("the tenants table is unreachable")
    answer = await _post(route, requested_by=HAL, acting_from=PARENT)
    assert answer.status_code == 404
    assert world.calls == []


@pytest.mark.parametrize("route", sorted(ROUTES))
async def test_a_role_in_the_body_is_never_believed(world, route) -> None:
    answer = await _post(route, requested_by=PAT, acting_from=PARENT, role="admin")
    assert answer.status_code == 404
    assert world.calls == []
