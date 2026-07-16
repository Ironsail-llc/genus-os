"""Tests for per-user webchat session-key derivation (Task 3, Unified Identity
Context — ROBOTHOR_PER_USER_SESSIONS).

Today every dashboard user shares ONE engine chat session
(``agent:main:primary``), and every chat endpoint accepts any session_key
from any authenticated same-tenant caller with no ownership check. This adds
``_effective_session_key(auth, requested_key)``, applied at every endpoint
that takes a session_key, so that:

- service-typ callers (ops tooling, the Telegram bridge) always keep the
  requested key verbatim, in every flag mode.
- the tenant owner always keeps the requested key verbatim, in every flag
  mode (webchat<->Telegram shared-session continuity must survive).
- non-owner, non-service ("member") callers get transparently isolated onto
  ``agent:{agent_id}:user:{user_id}`` — but ONLY when the flag is
  ``enforce``. ``observe`` computes and logs the would-be derivation without
  changing behavior. ``off`` (the default) is byte-identical to pre-flag
  behavior.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.auth.deps import AuthContext
from robothor.engine.chat import _effective_session_key, _sessions, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType


def _member_auth(user_id: str = "bob", role: str = "member") -> AuthContext:
    return AuthContext(user_id=user_id, tenant_id="test-tenant", role=role, typ="user")


def _owner_auth(user_id: str = "loopback-development-operator") -> AuthContext:
    return AuthContext(user_id=user_id, tenant_id="test-tenant", role="owner", typ="user")


def _service_auth(agent_id: str = "main") -> AuthContext:
    return AuthContext(
        user_id="svc-agent", tenant_id="test-tenant", role="service", typ="service", agent_id=agent_id
    )


# ─── Pure-function tests ──────────────────────────────────────────────


class TestEffectiveSessionKeyOff:
    """Default flag state — behavior must be byte-identical to today."""

    def test_member_unchanged_when_flag_unset(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_PER_USER_SESSIONS", raising=False)
        auth = _member_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_member_unchanged_when_flag_explicitly_off(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
        auth = _member_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_owner_unchanged(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
        auth = _owner_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_service_unchanged(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
        auth = _service_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"


class TestEffectiveSessionKeyEnforce:
    def test_member_gets_derived_key(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        auth = _member_auth(user_id="bob")
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:user:bob"

    def test_agent_id_parsed_from_requested_key(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        auth = _member_auth(user_id="bob")
        assert (
            _effective_session_key(auth, "agent:research:primary") == "agent:research:user:bob"
        )

    def test_owner_unchanged_even_in_enforce(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        auth = _owner_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_service_unchanged_even_in_enforce(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        auth = _service_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_different_members_get_different_keys(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        alice = _effective_session_key(_member_auth(user_id="alice"), "agent:main:primary")
        bob = _effective_session_key(_member_auth(user_id="bob"), "agent:main:primary")
        assert alice != bob
        assert alice == "agent:main:user:alice"
        assert bob == "agent:main:user:bob"

    def test_other_human_roles_are_treated_as_members(self, monkeypatch):
        """Only role == 'owner' is exempt — admin/user/viewer/auditor are isolated."""
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        for role in ("admin", "user", "viewer", "auditor"):
            auth = _member_auth(user_id="carol", role=role)
            assert _effective_session_key(auth, "agent:main:primary") == "agent:main:user:carol"

    def test_short_key_falls_back_to_default_agent(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        auth = _member_auth(user_id="bob")
        # No colon-separated agent segment — fall back to default_chat_agent ("main"
        # when _config is None, matching the same fallback the rest of chat.py uses).
        assert _effective_session_key(auth, "no-colons-here") == "agent:main:user:bob"


class TestEffectiveSessionKeyObserve:
    def test_member_key_unchanged(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "observe")
        auth = _member_auth(user_id="bob")
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_owner_key_unchanged(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "observe")
        auth = _owner_auth()
        assert _effective_session_key(auth, "agent:main:primary") == "agent:main:primary"

    def test_logs_would_be_derivation_for_member(self, monkeypatch, caplog):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "observe")
        auth = _member_auth(user_id="bob")
        with caplog.at_level(logging.INFO, logger="robothor.engine.chat"):
            _effective_session_key(auth, "agent:main:primary")
        assert any("would derive" in r.message for r in caplog.records)
        assert any("agent:main:user:bob" in r.message for r in caplog.records)

    def test_does_not_log_for_owner(self, monkeypatch, caplog):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "observe")
        auth = _owner_auth()
        with caplog.at_level(logging.INFO, logger="robothor.engine.chat"):
            _effective_session_key(auth, "agent:main:primary")
        assert not any("would derive" in r.message for r in caplog.records)

    def test_does_not_log_for_service(self, monkeypatch, caplog):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "observe")
        auth = _service_auth()
        with caplog.at_level(logging.INFO, logger="robothor.engine.chat"):
            _effective_session_key(auth, "agent:main:primary")
        assert not any("would derive" in r.message for r in caplog.records)


# ─── Endpoint integration tests ────────────────────────────────────────


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    return runner


@pytest.fixture
def chat_app(engine_config, mock_runner):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    yield app
    _sessions.clear()


@pytest.fixture
async def client(chat_app):
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _completed_run(text: str = "ok") -> AgentRun:
    return AgentRun(status=RunStatus.COMPLETED, output_text=text, trigger_type=TriggerType.WEBCHAT)


class TestSendEndpointIsolation:
    @pytest.mark.asyncio
    async def test_member_send_isolated_from_owner_in_enforce_mode(
        self, client, mock_runner, monkeypatch
    ):
        """Two callers passing the SAME requested session_key end up in
        different in-memory sessions once enforce mode is on: the owner's
        history is untouched by a member's traffic."""
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        mock_runner.execute = AsyncMock(return_value=_completed_run("member reply"))

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post(
                "/chat/send",
                json={"session_key": "agent:main:primary", "message": "hi from bob"},
            )
        assert res.status_code == 200

        # The shared key the owner/Telegram actually use was never touched.
        assert "agent:main:primary" not in _sessions or not _sessions["agent:main:primary"].history
        assert "agent:main:user:bob" in _sessions
        assert _sessions["agent:main:user:bob"].history[0]["content"] == "hi from bob"

    @pytest.mark.asyncio
    async def test_owner_send_uses_shared_key_in_enforce_mode(
        self, client, mock_runner, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        mock_runner.execute = AsyncMock(return_value=_completed_run("owner reply"))

        with patch("robothor.engine.chat._auth_context", return_value=_owner_auth()):
            res = await client.post(
                "/chat/send",
                json={"session_key": "agent:main:primary", "message": "hi from owner"},
            )
        assert res.status_code == 200
        assert "agent:main:primary" in _sessions
        assert _sessions["agent:main:primary"].history[0]["content"] == "hi from owner"

    @pytest.mark.asyncio
    async def test_service_send_uses_requested_key_in_enforce_mode(
        self, client, mock_runner, monkeypatch
    ):
        """The Telegram bridge / ops tooling writes into whatever session it
        names — never remapped, even in enforce mode."""
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        mock_runner.execute = AsyncMock(return_value=_completed_run("svc reply"))

        with patch("robothor.engine.chat._auth_context", return_value=_service_auth()):
            res = await client.post(
                "/chat/send",
                json={"session_key": "agent:main:primary", "message": "svc write"},
            )
        assert res.status_code == 200
        assert "agent:main:primary" in _sessions
        assert _sessions["agent:main:primary"].history[0]["content"] == "svc write"

    @pytest.mark.asyncio
    async def test_member_send_unchanged_when_flag_off(self, client, mock_runner, monkeypatch):
        """Byte-identical proof: default flag state routes a member's traffic
        into the literal requested key, same as pre-flag behavior."""
        monkeypatch.delenv("ROBOTHOR_PER_USER_SESSIONS", raising=False)
        mock_runner.execute = AsyncMock(return_value=_completed_run("member reply"))

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post(
                "/chat/send",
                json={"session_key": "agent:main:primary", "message": "hi from bob"},
            )
        assert res.status_code == 200
        assert "agent:main:user:bob" not in _sessions
        assert _sessions["agent:main:primary"].history[0]["content"] == "hi from bob"


class TestHistoryEndpointIsolation:
    @pytest.mark.asyncio
    async def test_member_reads_own_derived_history(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        mock_runner.execute = AsyncMock(return_value=_completed_run("member reply"))

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            await client.post(
                "/chat/send",
                json={"session_key": "agent:main:primary", "message": "hi from bob"},
            )
            res = await client.get("/chat/history?session_key=agent:main:primary")

        assert res.status_code == 200
        data = res.json()
        assert data["sessionKey"] == "agent:main:user:bob"
        assert data["messages"][0]["content"] == "hi from bob"

    @pytest.mark.asyncio
    async def test_member_cannot_read_owner_session_by_guessing_the_shared_key(
        self, client, mock_runner, monkeypatch
    ):
        """The ownership hole this task closes: a member passing the literal
        owner key no longer reads the owner's conversation."""
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        mock_runner.execute = AsyncMock(return_value=_completed_run("owner secret"))

        with patch("robothor.engine.chat._auth_context", return_value=_owner_auth()):
            await client.post(
                "/chat/send",
                json={"session_key": "agent:main:primary", "message": "owner secret msg"},
            )

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.get("/chat/history?session_key=agent:main:primary")

        assert res.status_code == 200
        messages = res.json()["messages"]
        assert not any("owner secret" in m.get("content", "") for m in messages)

    @pytest.mark.asyncio
    async def test_history_unchanged_when_flag_off(self, client, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_PER_USER_SESSIONS", raising=False)
        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.get("/chat/history?session_key=plain-key")
        assert res.status_code == 200
        assert res.json()["sessionKey"] == "plain-key"


class TestInjectEndpointIsolation:
    @pytest.mark.asyncio
    async def test_member_inject_goes_to_derived_session(self, client, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post(
                "/chat/inject",
                json={"session_key": "agent:main:primary", "message": "canvas prompt"},
            )
        assert res.status_code == 200
        assert "agent:main:user:bob" in _sessions
        assert "agent:main:primary" not in _sessions


class TestAbortEndpointIsolation:
    @pytest.mark.asyncio
    async def test_member_abort_targets_derived_session(self, client, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post("/chat/abort", json={"session_key": "agent:main:primary"})
        assert res.status_code == 200
        assert "agent:main:user:bob" in _sessions


class TestClearEndpointIsolation:
    @pytest.mark.asyncio
    async def test_member_clear_only_clears_own_derived_session(self, client, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")

        with patch("robothor.engine.chat._auth_context", return_value=_owner_auth()):
            await client.post(
                "/chat/inject",
                json={"session_key": "agent:main:primary", "message": "owner history"},
            )

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post("/chat/clear", json={"session_key": "agent:main:primary"})

        assert res.status_code == 200
        # Owner's session must survive a member's clear call.
        assert len(_sessions["agent:main:primary"].history) == 1


class TestExportEndpointIsolation:
    @pytest.mark.asyncio
    async def test_member_export_reflects_derived_session(self, client, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            await client.post(
                "/chat/inject",
                json={"session_key": "agent:main:primary", "message": "bob only"},
            )
            res = await client.get(
                "/chat/export?session_key=agent:main:primary&format=json"
            )
        assert res.status_code == 200
        data = res.json()
        assert data["session_key"] == "agent:main:user:bob"
        assert data["history"][0]["content"] == "bob only"


class TestPlanEndpointsIsolation:
    @pytest.mark.asyncio
    async def test_plan_start_isolates_member_session(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="plan text[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post(
                "/chat/plan/start",
                json={"session_key": "agent:main:primary", "message": "do a thing"},
            )
        assert res.status_code == 200
        assert "agent:main:user:bob" in _sessions
        assert _sessions["agent:main:user:bob"].active_plan is not None
        assert "agent:main:primary" not in _sessions

    @pytest.mark.asyncio
    async def test_plan_status_reads_derived_session(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="plan text[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            await client.post(
                "/chat/plan/start",
                json={"session_key": "agent:main:primary", "message": "do a thing"},
            )
            res = await client.get("/chat/plan/status?session_key=agent:main:primary")

        assert res.status_code == 200
        assert res.json()["active"] is True

    @pytest.mark.asyncio
    async def test_plan_approve_executes_in_derived_session(
        self, client, mock_runner, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        plan_run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="plan text[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        exec_run = AgentRun(
            status=RunStatus.COMPLETED, output_text="executed", trigger_type=TriggerType.WEBCHAT
        )
        mock_runner.execute = AsyncMock(side_effect=[plan_run, exec_run])

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            start_res = await client.post(
                "/chat/plan/start",
                json={"session_key": "agent:main:primary", "message": "do a thing"},
            )
            plan_id = _sessions["agent:main:user:bob"].active_plan.plan_id

            approve_res = await client.post(
                "/chat/plan/approve",
                json={"session_key": "agent:main:primary", "plan_id": plan_id},
            )

        assert start_res.status_code == 200
        assert approve_res.status_code == 200
        assert _sessions["agent:main:user:bob"].active_plan is None
        assert "agent:main:primary" not in _sessions

    @pytest.mark.asyncio
    async def test_plan_reject_targets_derived_session(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="plan text[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            await client.post(
                "/chat/plan/start",
                json={"session_key": "agent:main:primary", "message": "do a thing"},
            )
            plan_id = _sessions["agent:main:user:bob"].active_plan.plan_id
            res = await client.post(
                "/chat/plan/reject",
                json={"session_key": "agent:main:primary", "plan_id": plan_id},
            )

        assert res.status_code == 200
        assert _sessions["agent:main:user:bob"].active_plan is None

    @pytest.mark.asyncio
    async def test_plan_iterate_targets_derived_session(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        plan_run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="plan text[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        revise_run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="revised plan[PLAN_READY]",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(side_effect=[plan_run, revise_run])

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            await client.post(
                "/chat/plan/start",
                json={"session_key": "agent:main:primary", "message": "do a thing"},
            )
            plan_id = _sessions["agent:main:user:bob"].active_plan.plan_id
            res = await client.post(
                "/chat/plan/iterate",
                json={
                    "session_key": "agent:main:primary",
                    "plan_id": plan_id,
                    "feedback": "make it shorter",
                },
            )

        assert res.status_code == 200
        assert _sessions["agent:main:user:bob"].active_plan.plan_text == "revised plan"


class TestDeepEndpointsIsolation:
    @pytest.mark.asyncio
    async def test_deep_start_isolates_member_session(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
        run = AgentRun(
            status=RunStatus.COMPLETED, output_text="deep answer", trigger_type=TriggerType.WEBCHAT
        )
        mock_runner.execute_deep = AsyncMock(return_value=run)

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            res = await client.post(
                "/chat/deep/start",
                json={"session_key": "agent:main:primary", "query": "what's up"},
            )
        assert res.status_code == 200
        assert "agent:main:user:bob" in _sessions
        assert "agent:main:primary" not in _sessions

    @pytest.mark.asyncio
    async def test_deep_status_reads_derived_session(self, client, mock_runner, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")

        async def slow_execute_deep(**kwargs):
            import asyncio as _asyncio

            await _asyncio.sleep(0.05)
            return AgentRun(
                status=RunStatus.COMPLETED, output_text="deep answer", trigger_type=TriggerType.WEBCHAT
            )

        mock_runner.execute_deep = AsyncMock(side_effect=slow_execute_deep)

        with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
            await client.post(
                "/chat/deep/start",
                json={"session_key": "agent:main:primary", "query": "what's up"},
            )
            # Poll status against the SAME requested (shared) key — must
            # resolve to bob's own derived session, not error or 404.
            res = await client.get("/chat/deep/status?session_key=agent:main:primary")

        assert res.status_code == 200
