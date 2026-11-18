"""A service caller that names no session gets one of its own.

An automated probe sent by a service caller appeared in the owner's own chat
session. ``_effective_session_key`` read an empty key as the main session and
then kept a service token's key verbatim, so any service that omitted the
field wrote into the conversation the owner's webchat and Telegram share.

The rule now: a SERVICE caller with no key lands in
``agent:<agent>:service:<service id>``, stable per service identity, in every
per-user-sessions mode. A human owner with no key still gets the main
session, and any caller that names a key is honoured exactly as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.auth.deps import AuthContext
from robothor.engine.chat import _effective_session_key, _sessions, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType

MAIN = "agent:main:primary"


def _service(service_id: str = "svc-probe", agent_id: str | None = "main") -> AuthContext:
    return AuthContext(
        user_id=service_id,
        tenant_id="test-tenant",
        role="service",
        typ="service",
        agent_id=agent_id,
    )


def _owner() -> AuthContext:
    return AuthContext(user_id="alice", tenant_id="test-tenant", role="owner", typ="user")


@pytest.fixture(params=["off", "observe", "enforce"])
def mode(request, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", request.param)
    return request.param


class TestTheKey:
    def test_a_service_with_no_key_never_lands_in_the_main_session(self, mode):
        assert _effective_session_key(_service(), "") != MAIN

    def test_it_lands_in_a_session_of_its_own(self, mode):
        assert _effective_session_key(_service(), "") == "agent:main:service:svc-probe"

    def test_the_session_is_stable_per_service(self, mode):
        first = _effective_session_key(_service("svc-probe"), "")
        again = _effective_session_key(_service("svc-probe"), "")
        other = _effective_session_key(_service("svc-other"), "")
        assert first == again
        assert first != other

    def test_two_services_bound_to_one_agent_stay_apart(self, mode):
        """The token's agent binding is shared; its subject is the service."""
        a = _effective_session_key(_service("svc-a", agent_id="main"), "")
        b = _effective_session_key(_service("svc-b", agent_id="main"), "")
        assert a != b

    def test_a_service_id_cannot_reshape_the_key(self, mode):
        key = _effective_session_key(_service("svc:main:primary"), "")
        assert key.count(":") == 3
        assert key != MAIN

    def test_a_service_that_names_a_key_keeps_it(self, mode):
        assert _effective_session_key(_service(), MAIN) == MAIN
        assert _effective_session_key(_service(), "agent:main:bench-1") == "agent:main:bench-1"

    def test_the_owner_with_no_key_still_gets_the_main_session(self, mode):
        assert _effective_session_key(_owner(), "") == MAIN


# ─── through the endpoint ────────────────────────────────────────────


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.COMPLETED, output_text="pong", trigger_type=TriggerType.WEBCHAT
        )
    )
    return runner


@pytest.fixture
async def client(engine_config, mock_runner, isolated_plan_claims):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    _sessions.clear()


@pytest.mark.asyncio
async def test_a_probe_with_no_key_leaves_the_owners_session_untouched(
    client, mock_runner, monkeypatch
):
    monkeypatch.delenv("ROBOTHOR_PER_USER_SESSIONS", raising=False)
    with patch("robothor.engine.chat._auth_context", return_value=_service()):
        res = await client.post("/chat/send", json={"message": "ping"})

    assert res.status_code == 200
    assert MAIN not in _sessions or not _sessions[MAIN].history
    assert _sessions["agent:main:service:svc-probe"].history[0]["content"] == "ping"
    assert mock_runner.execute.await_args.kwargs["trigger_detail"] == (
        "webchat:agent:main:service:svc-probe"
    )
