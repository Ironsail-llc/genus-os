"""``POST /api/rooms/{id}/periods``: pick a period of a report room (story C3).

The route is a thin wrapper over ``robothor.rooms.periods.request_period``,
which is proven against real Postgres in ``robothor/rooms/tests``. Here: the
caller is the verified token and nothing the body says. The bridge calls with
its SERVICE token and names the person in ``requested_by``; the engine then
reads that person's role from their account (never from the body) and checks
their access itself. A human token's own identity wins over any
``requested_by``; the answer is the
contract's camelCase ``Edition``; each refusal has its own status, so the
dashboard can tell "no such room" from "not allowed" from "not over yet"; and the
request never blocks the engine's event loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from robothor.auth.deps import AuthContext
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.models import Edition
from robothor.rooms.periods import PeriodError, RoomRunUnavailable

ROOM = "7a0c0e8e-63c4-4c5b-9a43-0d6f6f0c2b11"
ALICE = "5b8e4a8a-0f6e-4e43-8f53-1f9d1d3c2a77"
PATH = f"/api/rooms/{ROOM}/periods"

EDITION_KEYS = {
    "id",
    "seq",
    "kind",
    "status",
    "preview",
    "periodStart",
    "periodEnd",
    "grain",
    "revision",
    "title",
    "summary",
    "blocks",
    "suggestions",
    "provenance",
    "createdAt",
    "publishedSeq",
    "error",
}


def _edition(status: str = "building") -> Edition:
    return Edition(
        tenant_id="test-tenant",
        id="0d1c7a3e-8a4b-4f0e-9c55-6a3d7c1e2f90",
        room_id=ROOM,
        seq=3,
        kind="edition",
        status=status,
        preview=False,
        period_start=date(2026, 11, 9),
        period_end=date(2026, 11, 15),
        grain="week",
        revision=2,
        title="Week 46",
        summary="",
        blocks=[],
        suggestions=[],
        provenance={},
        requested_by=ALICE,
        published_seq=2 if status == "published" else None,
        created_at=datetime(2026, 11, 21, 9, 30, tzinfo=UTC),
    )


BOB = "9c3f1d2e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
SERVICE = AuthContext(
    user_id="genus-bridge", tenant_id="test-tenant", role="service", typ="service"
)


@pytest.fixture
def accounts(monkeypatch):
    """``user_accounts`` rows by id, as ``get_account_by_id`` returns them."""
    rows: dict[str, dict[str, Any]] = {
        ALICE: {"id": ALICE, "tenant_id": "test-tenant", "role": "member", "status": "active"},
    }
    monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", lambda uid: rows.get(uid))
    return rows


class FakeRequest:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result if result is not None else (_edition(), True)
        self.error = error

    async def __call__(self, actor: Any, room_id: str, grain: str, anchor: Any) -> Any:
        self.calls.append((actor, room_id, grain, anchor))
        if self.error is not None:
            raise self.error
        return self.result


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


@pytest.fixture
def fake(monkeypatch):
    fake = FakeRequest()
    monkeypatch.setattr("robothor.rooms.periods.request_period", fake)
    return fake


async def test_a_started_run_answers_202_with_the_contract_edition(fake):
    response = await _post(_app(), {"grain": "week", "anchor": "2026-11-11"})

    assert response.status_code == 202
    body = response.json()
    assert set(body) == EDITION_KEYS
    assert body["periodStart"] == "2026-11-09"
    assert body["periodEnd"] == "2026-11-15"
    assert body["status"] == "building"
    assert body["createdAt"].startswith("2026-11-21T09:30")
    actor, room_id, grain, anchor = fake.calls[0]
    assert (room_id, grain, anchor) == (ROOM, "week", "2026-11-11")


async def test_an_existing_edition_answers_200(fake):
    fake.result = (_edition("published"), False)

    response = await _post(_app(), {"grain": "week", "anchor": "2026-11-11"})

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["publishedSeq"] == 2


async def test_the_anchor_may_be_left_out(fake):
    response = await _post(_app(), {"grain": "day"})

    assert response.status_code == 202
    assert fake.calls[0][3] is None


async def test_the_caller_is_the_token_not_the_body(fake):
    auth = AuthContext(user_id=ALICE, tenant_id="test-tenant", role="viewer", typ="user")

    await _post(_app(auth), {"grain": "day", "user_id": "someone-else", "role": "owner"})

    actor = fake.calls[0][0]
    assert (actor.user_id, actor.role, actor.tenant_id, actor.is_service) == (
        ALICE,
        "viewer",
        "test-tenant",
        False,
    )


async def test_a_human_tokens_identity_wins_over_requested_by(fake, accounts):
    await _post(_app(), {"grain": "day", "requested_by": BOB})

    assert fake.calls[0][0].user_id == ALICE


async def test_the_bridge_service_token_acts_for_requested_by_with_their_account_role(
    fake, accounts
):
    accounts[ALICE]["role"] = "viewer"

    response = await _post(
        _app(SERVICE), {"grain": "day", "anchor": "2026-11-20", "requested_by": ALICE}
    )

    assert response.status_code == 202
    actor = fake.calls[0][0]
    assert (actor.user_id, actor.role, actor.tenant_id, actor.is_service) == (
        ALICE,
        "viewer",
        "test-tenant",
        False,
    )


async def test_a_role_in_the_body_is_never_believed(fake, accounts):
    await _post(_app(SERVICE), {"grain": "day", "requested_by": ALICE, "role": "owner"})

    assert fake.calls[0][0].role == "member"


async def test_a_service_call_without_requested_by_is_refused(fake, accounts):
    response = await _post(_app(SERVICE), {"grain": "day"})

    assert response.status_code == 400
    assert response.json()["error"] == "requested_by_required"
    assert fake.calls == []


@pytest.mark.parametrize(
    "account",
    [
        None,  # no such account
        {"tenant_id": "test-tenant-other", "role": "owner", "status": "active"},
        {"tenant_id": "test-tenant", "role": "owner", "status": "disabled"},
    ],
)
async def test_a_requester_whose_account_does_not_stand_here_gets_404(fake, accounts, account):
    if account is None:
        accounts.pop(ALICE)
    else:
        accounts[ALICE] = {"id": ALICE, **account}

    response = await _post(_app(SERVICE), {"grain": "day", "requested_by": ALICE})

    assert response.status_code == 404
    assert fake.calls == []


async def test_a_malformed_requested_by_is_refused(fake, accounts):
    response = await _post(_app(SERVICE), {"grain": "day", "requested_by": "not-a-uuid"})

    assert response.status_code == 404
    assert fake.calls == []


async def test_the_account_lookup_runs_off_the_event_loop(fake, monkeypatch):
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor

    await warm_executor()
    probe = SlowProbe(
        asyncio.get_running_loop(),
        result={"id": ALICE, "tenant_id": "test-tenant", "role": "member", "status": "active"},
    )
    monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", probe)

    response = await _post(_app(SERVICE), {"grain": "day", "requested_by": ALICE})

    assert response.status_code == 202
    await probe.settle()
    probe.assert_loop_free()


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RoomNotFound(ROOM), 404, "room_not_found"),
        (RoomForbidden("viewer access does not allow 'request_period'"), 403, "forbidden"),
        (PeriodError("that day is not over yet"), 422, "invalid_period"),
        (RoomRunUnavailable("room runs are not available"), 503, "runs_unavailable"),
    ],
)
async def test_each_refusal_has_its_own_status(fake, error, status, code):
    fake.error = error

    response = await _post(_app(), {"grain": "day", "anchor": "2026-11-20"})

    assert response.status_code == status
    assert response.json()["error"] == code
    assert response.json()["message"]


def test_no_refusal_says_a_limit_was_reached():
    from robothor.rooms import periods

    assert not hasattr(periods, "OnDemandCapReached")


async def test_no_room_existence_leaks_in_a_404(fake):
    fake.error = RoomNotFound(ROOM)

    body = (await _post(_app(), {"grain": "day"})).json()

    assert ROOM not in body["message"]


@pytest.mark.parametrize("body", [{}, {"grain": 3}, {"anchor": "2026-11-20"}, []])
async def test_a_malformed_body_is_refused_before_anything_runs(fake, body):
    response = await _post(_app(), body)

    assert response.status_code == 422
    assert fake.calls == []


def test_the_engine_app_mounts_the_route():
    from robothor.engine.health import create_health_app

    config = MagicMock()
    config.tenant_id = "test-tenant"
    config.bot_token = ""
    config.port = 18800
    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(config, runner=None)

    paths = {
        (getattr(candidate, "path", ""), tuple(sorted(getattr(candidate, "methods", ()) or ())))
        for outer in app.routes
        for candidate in getattr(getattr(outer, "original_router", None), "routes", (outer,))
    }
    assert ("/api/rooms/{room_id}/periods", ("POST",)) in paths


def test_a_dashboard_member_token_may_call_it_and_a_read_token_may_not():
    """POST outside /chat and /api/admin needs ``engine:write``: the member and
    user roles carry it, a viewer's dashboard token does not."""
    from robothor.engine.auth import required_scope

    assert required_scope("POST", PATH) == "engine:write"


async def test_the_request_runs_off_the_event_loop(monkeypatch):
    """The data layer is synchronous: every call to it goes through a thread."""
    from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor
    from robothor.rooms import periods

    await warm_executor()
    probe = SlowProbe(asyncio.get_running_loop())
    monkeypatch.setattr(periods, "_claim", probe)
    probe.result = (_edition("published"), False, None)

    response = await _post(_app(), {"grain": "week", "anchor": "2026-11-11"})

    assert response.status_code == 200
    await probe.settle()
    probe.assert_loop_free()


# ─── through the engine's real authentication ───────────────────────────


@pytest.fixture
def secure_engine(monkeypatch, fake, accounts):
    """The whole engine app, with authentication on and nothing stubbed above it."""
    from robothor.auth import tokens
    from robothor.engine.health import create_health_app

    monkeypatch.delenv("GENUS_INSECURE_DEV_MODE", raising=False)
    monkeypatch.setenv("ROBOTHOR_ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "rooms-periods-test-key-that-is-at-least-32-bytes")
    tokens.reset_signing_key_cache()
    monkeypatch.setattr("robothor.auth.session_standing.stands", lambda *_: True)
    config = MagicMock()
    config.tenant_id = "test-tenant"
    config.bot_token = ""
    config.port = 18800
    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        app = create_health_app(config, runner=None)
    yield app
    tokens.reset_signing_key_cache()


async def _post_as(app: FastAPI, token: str, body: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(PATH, json=body, headers={"Authorization": f"Bearer {token}"})


def test_a_service_caller_needs_the_control_scope_here():
    from robothor.engine.auth import required_scope

    assert required_scope("POST", PATH, service=True) == "engine:control"
    assert required_scope("POST", f"/api/rooms/{ROOM}/run", service=True) == "engine:control"
    assert required_scope("POST", PATH) == "engine:write"
    # Only these two routes: a service token's scope elsewhere is unchanged.
    assert required_scope("POST", "/api/agents/x/runs", service=True) == "engine:write"


async def test_the_bridges_own_engine_credential_reaches_the_route(secure_engine, fake):
    from robothor.engine_control import control_token

    token = control_token("genus-bridge", "test-tenant", require_existing_key=False)

    response = await _post_as(secure_engine, token, {"grain": "day", "requested_by": ALICE})

    assert response.status_code == 202
    assert fake.calls[0][0].user_id == ALICE


async def test_another_service_cannot_act_for_a_person(secure_engine, fake):
    from robothor.engine_control import control_token

    token = control_token("genus-cli", "test-tenant", require_existing_key=False)

    response = await _post_as(secure_engine, token, {"grain": "day", "requested_by": ALICE})

    assert response.status_code == 403
    assert fake.calls == []


async def test_a_service_token_without_the_control_scope_is_refused(secure_engine, fake):
    from robothor.auth.tokens import issue_service_token
    from robothor.engine.auth import ENGINE_AUDIENCE

    token = issue_service_token(
        "genus-bridge", "test-tenant", audience=ENGINE_AUDIENCE, scopes=("engine:write",)
    )

    response = await _post_as(secure_engine, token, {"grain": "day", "requested_by": ALICE})

    assert response.status_code == 403
    assert fake.calls == []


async def test_a_members_own_token_still_reaches_the_route(secure_engine, fake):
    from robothor.auth.tokens import issue_access_token

    token = issue_access_token(ALICE, "test-tenant", "member")

    response = await _post_as(secure_engine, token, {"grain": "day", "requested_by": BOB})

    assert response.status_code == 202
    assert fake.calls[0][0].user_id == ALICE


# ─── no exception text in a response ────────────────────────────────────

SECRET = "connection to 10.0.0.7 failed at /srv/app/dal.py line 99"


@pytest.mark.parametrize("error", [RuntimeError(SECRET), ValueError(SECRET), KeyError(SECRET)])
async def test_an_unexpected_error_answers_a_generic_500(fake, error):
    fake.error = error

    response = await _post(_app(), {"grain": "day"})

    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"
    assert SECRET not in response.text
    assert "10.0.0.7" not in response.text


async def test_an_account_lookup_that_fails_answers_a_generic_500(fake, monkeypatch):
    def broken(_uid: str) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", broken)

    response = await _post(_app(SERVICE), {"grain": "day", "requested_by": ALICE})

    assert response.status_code == 500
    assert SECRET not in response.text


@pytest.mark.parametrize(
    "make",
    [
        lambda: PeriodError(SECRET, reason="not_over"),
        lambda: RoomForbidden(SECRET),
        lambda: RoomRunUnavailable(SECRET),
    ],
)
async def test_a_refusal_message_is_the_routes_own_text(fake, make):
    fake.error = make()

    response = await _post(_app(), {"grain": "day"})

    assert response.status_code in {403, 422, 503}
    assert SECRET not in response.text
    assert response.json()["message"]


async def test_an_invalid_period_names_its_reason(fake):
    fake.error = PeriodError("that day is not over yet", reason="not_over")

    body = (await _post(_app(), {"grain": "day"})).json()

    assert body["reason"] == "not_over"
    assert "not over" in body["message"]
