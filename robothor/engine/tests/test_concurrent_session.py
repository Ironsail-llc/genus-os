"""
E2E tests for concurrent Telegram + Helm chat on a shared session.

Simulates realistic concurrent usage against the full chat FastAPI app
(via ASGITransport — no real network). Uses a mock runner with
configurable delays to reproduce the exact conditions that caused
524/409 errors before the lock removal.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine.chat import _sessions, get_shared_session, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType


@pytest.fixture
def mock_runner(engine_config):
    """Create a mock AgentRunner."""
    runner = MagicMock()
    runner.config = engine_config
    return runner


@pytest.fixture
def chat_app(engine_config, mock_runner):
    """Create a FastAPI app with chat router mounted."""
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
    """Async HTTP client for testing."""
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def bot_session(engine_config):
    """Simulate Telegram's session sharing — ensure the canonical session exists
    and return it, so tests can manipulate it as Telegram would."""
    session = get_shared_session(engine_config.main_session_key)
    session.history.clear()
    session.model_override = None
    return session


async def _fire_chat(client: AsyncClient, session_key: str, message: str) -> tuple[int, str]:
    """Send a chat message and return (status_code, body_text)."""
    res = await client.post(
        "/chat/send",
        json={"session_key": session_key, "message": message},
    )
    return res.status_code, res.text


def _parse_sse(body: str) -> list[dict]:
    """Parse SSE text into a list of {event, data} dicts, skipping comments."""
    events = []
    current_event = ""
    for line in body.split("\n"):
        if line.startswith(":"):
            continue
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append({"event": current_event, "data": data})
    return events


@pytest.mark.e2e
class TestConcurrentChannels:
    @pytest.mark.asyncio
    async def test_helm_succeeds_while_telegram_executing(
        self, client, mock_runner, bot_session, engine_config
    ):
        """Core regression: Helm must NOT get 409 or hang when Telegram is active.

        Before fix: Telegram holds session.lock for ~50s, Helm returns 409.
        After fix: Both execute concurrently, both return 200 with valid SSE.
        """
        call_count = 0

        async def fake_execute(**kwargs):
            nonlocal call_count
            call_count += 1
            n = call_count
            await asyncio.sleep(0.1 if n == 1 else 0.05)
            on_content = kwargs.get("on_content")
            text = f"reply-{n}"
            if on_content:
                await on_content(text)
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text=text,
                trigger_type=TriggerType.WEBCHAT,
            )

        mock_runner.execute = AsyncMock(side_effect=fake_execute)
        session_key = engine_config.main_session_key

        # Fire both concurrently — same session key (shared session)
        (status1, body1), (status2, body2) = await asyncio.gather(
            _fire_chat(client, session_key, "from telegram"),
            _fire_chat(client, session_key, "from helm"),
        )

        assert status1 == 200, f"First request failed: {status1}"
        assert status2 == 200, f"Second request failed: {status2}"

        # Both should have valid done events
        events1 = _parse_sse(body1)
        events2 = _parse_sse(body2)
        done1 = [e for e in events1 if e["event"] == "done"]
        done2 = [e for e in events2 if e["event"] == "done"]
        assert len(done1) == 1
        assert len(done2) == 1

    @pytest.mark.asyncio
    async def test_keepalive_prevents_timeout_on_slow_response(self, client, mock_runner):
        """SSE keepalives are emitted during slow agent execution.

        Before fix: Cloudflare 524 after 100s of silence.
        After fix: `: keepalive` comments every 15s keep the connection alive.
        """

        async def slow_execute(**kwargs):
            await asyncio.sleep(0.5)
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text="finally",
                trigger_type=TriggerType.WEBCHAT,
            )

        mock_runner.execute = AsyncMock(side_effect=slow_execute)

        with patch("robothor.engine.chat.SSE_KEEPALIVE_INTERVAL", 0.1):
            status, body = await _fire_chat(client, "keepalive:main:e2e", "slow request")

        assert status == 200
        assert ": keepalive" in body
        events = _parse_sse(body)
        done = [e for e in events if e["event"] == "done"]
        assert len(done) == 1
        assert done[0]["data"]["text"] == "finally"

    @pytest.mark.asyncio
    async def test_shared_history_consistent_after_concurrent_use(
        self, client, mock_runner, bot_session, engine_config
    ):
        """Both channels' messages appear in shared history after concurrent execution."""
        counter = 0

        async def fake_execute(**kwargs):
            nonlocal counter
            counter += 1
            msg = kwargs.get("message", "")
            await asyncio.sleep(0.02)
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text=f"re: {msg}",
                trigger_type=TriggerType.WEBCHAT,
            )

        mock_runner.execute = AsyncMock(side_effect=fake_execute)
        session_key = engine_config.main_session_key

        await asyncio.gather(
            _fire_chat(client, session_key, "telegram-msg"),
            _fire_chat(client, session_key, "helm-msg"),
        )

        # Check history
        res = await client.get(f"/chat/history?session_key={session_key}")
        data = res.json()
        contents = [m["content"] for m in data["messages"]]

        assert "telegram-msg" in contents
        assert "helm-msg" in contents
        assert "re: telegram-msg" in contents
        assert "re: helm-msg" in contents
        assert len(data["messages"]) == 4

    @pytest.mark.asyncio
    async def test_no_history_corruption_under_rapid_fire(self, client, mock_runner):
        """Rapid sequential messages don't lose or corrupt history entries."""
        counter = 0

        async def fake_execute(**kwargs):
            nonlocal counter
            counter += 1
            msg = kwargs.get("message", "")
            await asyncio.sleep(0.01)
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text=f"reply-{msg}",
                trigger_type=TriggerType.WEBCHAT,
            )

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        # Send 5 messages rapidly
        tasks = [_fire_chat(client, "rapid:main:e2e", f"msg-{i}") for i in range(5)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        for status, _ in results:
            assert status == 200

        # Check history has all 10 entries
        res = await client.get("/chat/history?session_key=rapid:main:e2e")
        messages = res.json()["messages"]
        assert len(messages) == 10

        # Verify each user message has a matching assistant reply
        user_msgs = [m for m in messages if m["role"] == "user"]
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(user_msgs) == 5
        assert len(asst_msgs) == 5

    @pytest.mark.asyncio
    async def test_model_override_visible_across_channels(
        self, client, mock_runner, bot_session, engine_config
    ):
        """Model override set by one channel is used by the other."""
        # Simulate Telegram setting model override on the shared session
        bot_session.model_override = "anthropic/claude-sonnet-4-6"

        captured_override = None

        async def capture_execute(**kwargs):
            nonlocal captured_override
            captured_override = kwargs.get("model_override")
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text="ok",
                trigger_type=TriggerType.WEBCHAT,
            )

        mock_runner.execute = AsyncMock(side_effect=capture_execute)
        session_key = engine_config.main_session_key

        await _fire_chat(client, session_key, "helm message")

        assert captured_override == "anthropic/claude-sonnet-4-6"
