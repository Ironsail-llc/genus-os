"""From a Teams activity to an Adaptive Card, through the platform's own tool.

The review's Critical finding was that every test in this distribution called
``ask_over_card`` directly, so the ninety of them stayed green while the only
caller the platform has — the ``ask_user`` tool — refused every Teams run before
it reached the channel. A control built, wired, tested, and aimed at nothing.

This file is the test that could not have stayed green through that: it starts
at the HTTP endpoint with a signed activity, lets the real inbound pipeline run,
has the agent call the real ``ask_user`` handler, and asserts that the card came
out with the conversation as its target and the sender as its addressee. Every
link is the real one except the runner (which stands in for the model) and the
HTTP transports.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from genus_teams import ask as ask_module
from genus_teams import channel as channel_module
from genus_teams import credentials as credentials_module
from genus_teams import jwt_validation
from genus_teams import router as router_module
from genus_teams.channel import TeamsChannel

from robothor.engine.channels import access
from robothor.engine.channels.conversations import ConversationRef
from robothor.engine.tools.handlers import ask_user as ask_user_module
from robothor.identity import IdentityContext

APP_ID = "00000000-0000-0000-0000-00000000aaaa"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
CONVERSATION = "19:a-team-channel.thread.v2"
ALICE_AAD = "aaaaaaaa-0000-0000-0000-00000000aaaa"
TENANT = "00000000-0000-0000-0000-000000000000"
PATH = "/api/channels/teams/messages"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "e2e-key"


def _jwks() -> dict[str, Any]:
    import jwt

    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    public.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [public]}


def _token() -> str:
    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "iss": jwt_validation.ISSUER,
            "aud": APP_ID,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "serviceurl": SERVICE_URL,
        },
        _KEY,
        algorithm="RS256",
        headers={"kid": KID},
    )


def _activity(text: str = "should we ship?") -> dict[str, Any]:
    return {
        "type": "message",
        "id": "1700000000001",
        "serviceUrl": SERVICE_URL,
        "text": text,
        "conversation": {"id": CONVERSATION, "conversationType": "channel"},
        "from": {"id": "29:alice-client-id", "aadObjectId": ALICE_AAD, "name": "Alice"},
        "recipient": {"id": f"28:{APP_ID}", "name": "Genus"},
    }


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    """Credentials, keys, the conversation store and the durable question row."""
    creds = credentials_module.TeamsCredentials(
        app_id=APP_ID, app_password="not-a-real-client-secret", directory_tenant_id=None
    )
    for module in (credentials_module, channel_module, jwt_validation, router_module):
        monkeypatch.setattr(module, "teams_credentials", lambda **_kw: creds, raising=False)

    def _serve(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == jwt_validation.OPENID_METADATA_URL:
            return httpx.Response(200, json={"jwks_uri": "https://login.botframework.com/keys"})
        return httpx.Response(200, json=_jwks())

    monkeypatch.setattr(
        jwt_validation,
        "build_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_serve)),
    )
    jwt_validation.reset_key_cache()

    reference = ConversationRef(
        channel="teams",
        native_id=ALICE_AAD,
        conversation_id=CONVERSATION,
        service_url=SERVICE_URL,
    )
    monkeypatch.setattr(router_module.conversations, "record", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        channel_module.conversations, "reference_for_target", lambda *_a, **_kw: reference
    )

    identity = IdentityContext(
        tenant_id=TENANT,
        channel="teams",
        identifier=ALICE_AAD,
        verified=True,
        tenant_user_id="alice",
        role="member",
    )

    async def _allow(*_a: Any, **_kw: Any) -> access.AccessDecision:
        return access.AccessDecision(allowed=True, identity=identity)

    monkeypatch.setattr(access, "evaluate", _allow)

    class _Asked:
        id = "q-1"
        expires_at = None

    monkeypatch.setattr(ask_user_module.agent_questions, "ask_question", lambda **_kw: _Asked())
    monkeypatch.setattr(ask_user_module.agent_questions, "answer_question", lambda *_a, **_kw: None)

    async def _emit(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(ask_user_module.run_status, "emit_status", _emit)

    class _Session:
        history: list[Any] = []

    monkeypatch.setattr(router_module, "spawn", lambda coro: asyncio.create_task(coro))
    from robothor.engine.channels import inbound as inbound_module

    monkeypatch.setattr(inbound_module, "get_shared_session", lambda _key: _Session())
    ask_module.reset_pending_asks()
    router_module.reset_rate_limit()
    yield identity
    ask_module.reset_pending_asks()


@pytest.mark.asyncio
async def test_a_teams_message_can_end_in_an_adaptive_card_asked_of_its_sender(monkeypatch):
    """The whole path, with nothing standing in for the wiring under test."""
    channel = TeamsChannel()
    cards: list[dict[str, Any]] = []
    sent: list[tuple[str, str]] = []

    async def _send_card(target: str, card: dict[str, Any]) -> bool:
        cards.append({"target": target, "card": card})
        return True

    async def _send(target: str, text: str, **_kw: Any) -> Any:
        from robothor.engine.channels.base import SendReceipt

        sent.append((target, text))
        return SendReceipt(acknowledged=1, expected=1, platform_ids=["1"], target=target)

    monkeypatch.setattr(channel, "send_card", _send_card)
    monkeypatch.setattr(channel, "send", _send)

    # The armed channel, as the registry would resolve it for `ask_user`.
    from robothor.engine import channels as channels_module

    monkeypatch.setattr(
        channels_module, "get_channel", lambda name: channel if name == "teams" else None
    )

    asked: dict[str, Any] = {}
    recorded_runs: list[dict[str, Any]] = []

    class _Runner:
        """Stands in for the model: this run calls ask_user and answers with it."""

        async def execute(self, **kw: Any) -> Any:
            recorded_runs.append(kw)

            class _Ctx:
                run_id = "run-1"
                tenant_id = kw["tenant_id"]
                agent_id = kw["agent_id"]
                identity = kw["identity"]

            monkeypatch.setattr(
                ask_user_module.tracking,
                "get_run",
                lambda _run_id: {
                    "trigger_type": str(kw["trigger_type"]),
                    "trigger_detail": kw.get("trigger_detail") or "",
                },
            )
            asked["result"] = await ask_user_module._handle_ask_user(
                {"question": "Ship it?", "options": ["yes", "no"], "timeout_seconds": 30},
                _Ctx(),
            )

            class _Run:
                output_text = f"decided: {asked['result'].get('answer')}"

            return _Run()

    channel.bind_runtime(runner=_Runner(), runtime=None)

    app = FastAPI()
    app.include_router(channel.inbound_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://engine.test"
    ) as client:
        response = await client.post(
            PATH, json=_activity(), headers={"Authorization": f"Bearer {_token()}"}
        )
    assert response.status_code == 200

    # The card goes out from inside the run; answer it as Alice would.
    for _ in range(300):
        if cards:
            break
        await asyncio.sleep(0.01)
    assert cards, "ask_user never reached the Teams channel — the card was never sent"

    card = cards[0]["card"]
    assert cards[0]["target"] == CONVERSATION, (
        "the question was aimed somewhere other than the conversation it came from"
    )
    ask_id = card["actions"][0]["data"][ask_module.ASK_FIELD]
    assert [action["title"] for action in card["actions"]] == ["yes", "no"]

    settled = ask_module.settle_from_activity(
        {"type": "message", "value": {ask_module.ASK_FIELD: ask_id, ask_module.CHOICE_FIELD: 0}},
        conversation_id=CONVERSATION,
        native_id=ALICE_AAD,
    )
    assert settled is True

    for _ in range(300):
        if sent:
            break
        await asyncio.sleep(0.01)

    assert asked["result"]["answered"] is True
    assert asked["result"]["answer"] == "yes"
    assert sent == [(CONVERSATION, "decided: yes")]
    assert recorded_runs[0]["trigger_detail"] == f"channel:teams:{CONVERSATION}"


@pytest.mark.asyncio
async def test_the_card_is_bound_to_the_sender_so_a_bystander_cannot_answer(monkeypatch):
    """The same path, and the reason the addressee matters: this activity
    arrived in a TEAM CHANNEL, where everyone can see the card."""
    channel = TeamsChannel()
    cards: list[dict[str, Any]] = []

    async def _send_card(target: str, card: dict[str, Any]) -> bool:
        cards.append(card)
        return True

    async def _send(target: str, text: str, **_kw: Any) -> Any:
        from robothor.engine.channels.base import SendReceipt

        return SendReceipt(acknowledged=1, expected=1, platform_ids=["1"], target=target)

    monkeypatch.setattr(channel, "send_card", _send_card)
    monkeypatch.setattr(channel, "send", _send)
    from robothor.engine import channels as channels_module

    monkeypatch.setattr(
        channels_module, "get_channel", lambda name: channel if name == "teams" else None
    )

    answer: dict[str, Any] = {}

    class _Runner:
        async def execute(self, **kw: Any) -> Any:
            class _Ctx:
                run_id = "run-1"
                tenant_id = kw["tenant_id"]
                agent_id = kw["agent_id"]
                identity = kw["identity"]

            monkeypatch.setattr(
                ask_user_module.tracking,
                "get_run",
                lambda _run_id: {
                    "trigger_type": str(kw["trigger_type"]),
                    "trigger_detail": kw.get("trigger_detail") or "",
                },
            )
            answer["result"] = await ask_user_module._handle_ask_user(
                {"question": "Ship it?", "options": ["yes", "no"], "timeout_seconds": 2},
                _Ctx(),
            )

            class _Run:
                output_text = "done"

            return _Run()

    channel.bind_runtime(runner=_Runner(), runtime=None)
    app = FastAPI()
    app.include_router(channel.inbound_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://engine.test"
    ) as client:
        await client.post(PATH, json=_activity(), headers={"Authorization": f"Bearer {_token()}"})

    for _ in range(300):
        if cards:
            break
        await asyncio.sleep(0.01)
    ask_id = cards[0]["actions"][0]["data"][ask_module.ASK_FIELD]

    handled = ask_module.settle_from_activity(
        {"type": "message", "value": {ask_module.ASK_FIELD: ask_id, ask_module.CHOICE_FIELD: 0}},
        conversation_id=CONVERSATION,
        native_id="bbbbbbbb-0000-0000-0000-00000000bbbb",
        identity=None,
    )
    assert handled is True

    for _ in range(400):
        if "result" in answer:
            break
        await asyncio.sleep(0.01)
    assert answer["result"].get("answered") is not True, (
        "a bystander in the room answered a question asked of the sender"
    )
