"""Teams: ``/steer`` and ``/stop`` typed to the bot, and a progress activity.

Behind ``ROBOTHOR_STEER_EVERYWHERE``. Off, ``/steer`` is an ordinary sentence
the agent runs, as it always was.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import pytest
from genus_teams import channel as channel_module
from genus_teams import credentials as credentials_module
from genus_teams import router as router_module
from genus_teams import tokens as tokens_module
from genus_teams.channel import TeamsChannel
from robothor.engine import live_control
from robothor.engine.channels import access, inbound
from robothor.engine.channels.access import AccessDecision

CONVERSATION = "19:meeting_abc@thread.v2"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
ALICE = "29:alice-object-id"
SESSION = f"agent:main:teams:{CONVERSATION}"


@pytest.fixture
def flag(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(live_control, "steer_everywhere_enabled", lambda: state["on"])
    return state


@pytest.fixture(autouse=True)
def gate(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"decision": AccessDecision(allowed=True)}

    async def _evaluate(channel: str, native_id: str, **kw: Any) -> AccessDecision:
        return state["decision"]

    monkeypatch.setattr(access, "evaluate", _evaluate)
    return state


@pytest.fixture
def controls(monkeypatch) -> dict[str, list]:
    calls: dict[str, list] = {"steer": [], "interrupt": []}

    def _steer(key: str, text: str, *, tenant_id: str) -> bool:
        calls["steer"].append((key, text))
        return True

    def _interrupt(key: str, message: str | None = None, *, tenant_id: str) -> bool:
        calls["interrupt"].append(key)
        return True

    monkeypatch.setattr(live_control, "steer_conversation", _steer)
    monkeypatch.setattr(live_control, "interrupt_conversation", _interrupt)
    return calls


class _Channel:
    name = "teams"
    tenant_id = "test-tenant"

    def __init__(self) -> None:
        self.runner = object()
        self.sent: list[tuple[str, str]] = []
        self.progress_posts: list[tuple[str, str]] = []
        self.progress_updates: list[tuple[str, str, str]] = []

    async def send(self, target: str, text: str, **kw: Any) -> None:
        self.sent.append((target, text))

    async def post_progress(self, conversation_id: str, text: str) -> str | None:
        self.progress_posts.append((conversation_id, text))
        return "activity-1"

    async def update_progress(self, conversation_id: str, activity_id: str, text: str) -> None:
        self.progress_updates.append((conversation_id, activity_id, text))


@pytest.fixture
def runs(monkeypatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def _handle_message(**kw: Any) -> Any:
        seen.append(kw)
        on_progress = kw.get("on_progress")
        if on_progress is not None:
            await on_progress({"event": "tool_start", "tool": "web_search"})
            await asyncio.sleep(0.05)
        return inbound.InboundResult(reply="The answer.", ran=True, kind="answer")

    monkeypatch.setattr(inbound, "handle_message", _handle_message)
    return seen


async def _say(channel: _Channel, text: str) -> None:
    await router_module._handle(
        channel,
        text=text,
        native_id=ALICE,
        display_name="Alice",
        conversation_id=CONVERSATION,
        surface=access.DIRECT_SURFACE,
    )


@pytest.mark.asyncio
async def test_steer_reaches_the_conversations_run_and_starts_no_turn(flag, controls, runs):
    channel = _Channel()
    await _say(channel, "/steer use the EU region")
    assert controls["steer"] == [(SESSION, "use the EU region")]
    assert runs == [], "a steer is not a new turn"
    assert channel.sent == [(CONVERSATION, "Steering the run.")]


@pytest.mark.asyncio
async def test_stop_interrupts_the_conversations_run(flag, controls, runs):
    channel = _Channel()
    await _say(channel, "/stop")
    assert controls["interrupt"] == [SESSION]
    assert runs == []
    assert channel.sent == [(CONVERSATION, "Stopping the run.")]


@pytest.mark.asyncio
async def test_a_refused_sender_steers_nothing(flag, controls, runs, gate):
    gate["decision"] = AccessDecision(allowed=False, refusal="")
    channel = _Channel()
    await _say(channel, "/steer x")
    assert controls["steer"] == [] and runs == [] and channel.sent == []


@pytest.mark.asyncio
async def test_flag_off_steer_is_an_ordinary_message(flag, controls, runs):
    flag["on"] = False
    channel = _Channel()
    await _say(channel, "/steer use the EU region")
    assert controls["steer"] == []
    assert len(runs) == 1 and runs[0]["text"] == "/steer use the EU region"
    assert "on_progress" not in runs[0]
    assert channel.progress_posts == []


@pytest.mark.asyncio
async def test_flag_on_a_run_posts_one_progress_activity_and_updates_it(flag, runs):
    channel = _Channel()
    await _say(channel, "search it")
    assert len(channel.progress_posts) == 1
    assert channel.progress_updates, "updated in place, not posted again"
    assert all(u[1] == "activity-1" for u in channel.progress_updates)
    assert channel.sent == [(CONVERSATION, "The answer.")]


# ── the channel's half: post, then PUT the same activity ──────────────────


@pytest.fixture
def http(monkeypatch):
    from robothor.engine.channels.conversations import ConversationRef

    creds = credentials_module.TeamsCredentials(
        app_id="00000000-0000-0000-0000-00000000aaaa",
        app_password="not-a-real-client-secret",
        directory_tenant_id=None,
    )
    for module in (credentials_module, channel_module, tokens_module):
        monkeypatch.setattr(module, "teams_credentials", lambda **_kw: creds)
    ref = ConversationRef(
        channel="teams", native_id=ALICE, conversation_id=CONVERSATION, service_url=SERVICE_URL
    )
    monkeypatch.setattr(
        channel_module.conversations, "reference_for_target", lambda *_a, **_kw: ref
    )
    requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if (urlsplit(str(request.url)).hostname or "") == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 3599})
        return httpx.Response(200, json={"id": "activity-9"})

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_handle))

    monkeypatch.setattr(tokens_module, "build_client", _client)
    monkeypatch.setattr(channel_module, "build_client", _client)
    return requests


@pytest.mark.asyncio
async def test_the_progress_activity_is_posted_then_updated_in_place(http):
    channel = TeamsChannel()
    activity_id = await channel.post_progress(CONVERSATION, "Step 1")
    assert activity_id == "activity-9"
    await channel.update_progress(CONVERSATION, activity_id, "Step 2")

    calls = [r for r in http if (urlsplit(str(r.url)).hostname or "") != "login.microsoftonline.com"]
    assert [r.method for r in calls] == ["POST", "PUT"]
    conv = quote(CONVERSATION, safe="")
    assert str(calls[1].url).endswith(f"/v3/conversations/{conv}/activities/activity-9")
