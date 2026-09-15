"""The messaging endpoint: what it refuses, and what it does with what is left.

This is the only route in this distribution reachable from the public internet,
so the first half of this file is about refusal. Every token is minted HERE, in
the test, from a key pair generated in this process — the Bot Framework's
published keys are served by a fixture, and nothing contacts Microsoft.

The refusals, in the order an attacker tries them:

* a token signed with the **wrong key** (the forgery that a metadata document
  fetched over TLS is supposed to stop);
* a token for **somebody else's bot** (``aud`` is another application id);
* an **expired** token;
* a token from the **wrong issuer**;
* a valid token **replayed against a different serviceUrl** — the swap that
  would make the bot POST its next reply, bearer token attached, at an endpoint
  of the attacker's choosing.

All of them 401, and none of them say which one it was: an error that
distinguishes "wrong audience" from "bad signature" is an oracle.

Then: a size cap, a rate limit, and the two things a valid activity actually
does — record the conversation reference, and drive the SHARED inbound pipeline
exactly once.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from genus_teams import channel as channel_module
from genus_teams import credentials as credentials_module
from genus_teams import jwt_validation
from genus_teams import router as router_module
from genus_teams.channel import TeamsChannel

APP_ID = "00000000-0000-0000-0000-00000000aaaa"
OTHER_APP_ID = "00000000-0000-0000-0000-00000000cccc"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
EVIL_SERVICE_URL = "https://attacker.example.com/"
CONVERSATION = "19:meeting_abc@thread.v2"
ALICE_OBJECT_ID = "29:alice-object-id"
ALICE_AAD = "aaaaaaaa-0000-0000-0000-00000000aaaa"
PATH = "/api/channels/teams/messages"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key-1"


def _jwks() -> dict[str, Any]:
    """The Bot Framework's published keys, in the shape its JWKS endpoint uses."""
    import jwt

    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    public.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [public]}


def _token(
    *,
    key: Any = _KEY,
    aud: str = APP_ID,
    iss: str = jwt_validation.ISSUER,
    service_url: str = SERVICE_URL,
    expires_in: int = 300,
    kid: str | None = KID,
) -> str:
    import jwt

    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in,
        "serviceurl": service_url,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _activity(**overrides: Any) -> dict[str, Any]:
    activity = {
        "type": "message",
        "id": "1700000000001",
        "serviceUrl": SERVICE_URL,
        "text": "hello",
        "conversation": {"id": CONVERSATION, "conversationType": "personal"},
        "from": {"id": ALICE_OBJECT_ID, "aadObjectId": ALICE_AAD, "name": "Alice"},
        "recipient": {"id": f"28:{APP_ID}", "name": "Genus"},
    }
    activity.update(overrides)
    return activity


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    creds = credentials_module.TeamsCredentials(
        app_id=APP_ID, app_password="not-a-real-client-secret", directory_tenant_id=None
    )
    for module in (credentials_module, channel_module, jwt_validation, router_module):
        monkeypatch.setattr(module, "teams_credentials", lambda **_kw: creds, raising=False)
    return creds


@pytest.fixture(autouse=True)
def _served_keys(monkeypatch):
    """The OpenID metadata and the JWKS, from a fixture. Never Microsoft."""
    calls = {"metadata": 0, "keys": 0}

    def _handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == jwt_validation.OPENID_METADATA_URL:
            calls["metadata"] += 1
            return httpx.Response(200, json={"jwks_uri": "https://login.botframework.com/keys"})
        if url == "https://login.botframework.com/keys":
            calls["keys"] += 1
            return httpx.Response(200, json=_jwks())
        return httpx.Response(404)

    monkeypatch.setattr(
        jwt_validation,
        "build_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handle)),
    )
    jwt_validation.reset_key_cache()
    yield calls
    jwt_validation.reset_key_cache()


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kw: Any) -> Any:
        self.calls.append(kw)

        class _Run:
            output_text = "the answer"
            id = "run-1"

        return _Run()


@pytest.fixture
def recorded(monkeypatch):
    """What the endpoint wrote to the conversation store."""
    written: list[dict[str, Any]] = []

    def _record(channel: str, native_id: str, **kw: Any) -> None:
        written.append({"channel": channel, "native_id": native_id, **kw})

    monkeypatch.setattr(router_module.conversations, "record", _record)
    return written


@pytest.fixture
def sent(monkeypatch):
    """Replies the channel was asked to send, instead of an outbound activity."""
    outbound: list[tuple[str, str]] = []

    async def _send(self: Any, target: str, text: str, **kw: Any) -> Any:
        outbound.append((target, text))
        from robothor.engine.channels.base import SendReceipt

        return SendReceipt(acknowledged=1, expected=1, platform_ids=["1"], target=target)

    monkeypatch.setattr(TeamsChannel, "send", _send)
    return outbound


@pytest.fixture
def allow_everyone(monkeypatch):
    from robothor.engine.channels import access

    async def _evaluate(*_a: Any, **_kw: Any) -> access.AccessDecision:
        return access.AccessDecision(allowed=True)

    monkeypatch.setattr(access, "evaluate", _evaluate)


@pytest.fixture
def client(monkeypatch, recorded, sent):
    """The endpoint, mounted exactly as the engine mounts it."""
    from robothor.engine.channels.routers import mount_plugin_channel_routers
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_CHANNELS", "teams")
    reset_settings()

    channel = TeamsChannel()
    runner = _Runner()
    channel.bind_runtime(runner=runner, config=None)

    app = FastAPI()
    app.include_router(channel.inbound_router)
    router_module.reset_rate_limit()

    # Background work runs inline, so a test can see what the endpoint did.
    # The endpoint still ACKS first; this only removes the scheduling gap.
    started: list[Any] = []

    def _spawn(coro: Any) -> Any:
        started.append(coro)
        return None

    monkeypatch.setattr(router_module, "spawn", _spawn)

    with TestClient(app) as test_client:
        test_client.runner = runner  # type: ignore[attr-defined]
        test_client.channel = channel  # type: ignore[attr-defined]
        test_client.pending = started  # type: ignore[attr-defined]
        test_client.mounted = mount_plugin_channel_routers  # type: ignore[attr-defined]
        yield test_client

    for coro in started:
        coro.close()
    reset_settings()


def _drain(client: Any) -> None:
    """Run whatever the endpoint deferred, as the event loop would have."""
    import asyncio

    pending, client.pending[:] = list(client.pending), []
    for coro in pending:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _post(client: Any, activity: dict[str, Any] | None = None, token: str | None = None):
    return client.post(
        PATH,
        json=activity if activity is not None else _activity(),
        headers={"Authorization": f"Bearer {token if token is not None else _token()}"},
    )


class TestEveryForgeryIsRefused:
    def test_a_token_signed_with_the_wrong_key_is_401(self, client):
        response = _post(client, token=_token(key=_OTHER_KEY))
        assert response.status_code == 401

    def test_a_token_for_another_bot_is_401(self, client):
        assert _post(client, token=_token(aud=OTHER_APP_ID)).status_code == 401

    def test_an_expired_token_is_401(self, client):
        assert _post(client, token=_token(expires_in=-600)).status_code == 401

    def test_a_token_from_another_issuer_is_401(self, client):
        assert _post(client, token=_token(iss="https://login.example.com/")).status_code == 401

    def test_an_unsigned_token_is_401(self, client):
        """`alg: none` is the first thing anybody tries."""
        import jwt

        none_token = jwt.encode({"aud": APP_ID}, key="", algorithm="none")
        assert _post(client, token=none_token).status_code == 401

    def test_a_token_naming_an_unknown_key_is_401(self, client):
        assert _post(client, token=_token(kid="not-a-published-key")).status_code == 401

    def test_no_authorization_header_at_all_is_401(self, client):
        assert client.post(PATH, json=_activity()).status_code == 401

    def test_the_refusal_says_nothing_about_which_check_failed(self, client):
        """An error that distinguishes a bad signature from a wrong audience is
        an oracle: it tells whoever is probing which half to keep working on."""
        bodies = {
            _post(client, token=_token(key=_OTHER_KEY)).text,
            _post(client, token=_token(aud=OTHER_APP_ID)).text,
            _post(client, token=_token(expires_in=-600)).text,
        }
        assert len(bodies) == 1, f"the 401s are distinguishable: {bodies}"
        assert "signature" not in "".join(bodies).lower()
        assert "audience" not in "".join(bodies).lower()

    def test_a_refused_request_never_reaches_the_runner_or_the_store(self, client, recorded):
        _post(client, token=_token(key=_OTHER_KEY))
        _drain(client)
        assert client.runner.calls == []
        assert recorded == []


class TestTheServiceUrlIsBoundToTheSignedClaim:
    def test_a_valid_token_replayed_with_a_swapped_service_url_is_401(self, client):
        """The whole point of the ``serviceurl`` claim. Without this check, a
        replayed activity would have the bot POST its reply — bearer token
        attached — wherever the body said."""
        response = _post(client, activity=_activity(serviceUrl=EVIL_SERVICE_URL))
        assert response.status_code == 401

    def test_the_swapped_url_is_never_recorded(self, client, recorded):
        _post(client, activity=_activity(serviceUrl=EVIL_SERVICE_URL))
        _drain(client)
        assert recorded == []

    def test_a_trailing_slash_difference_is_not_a_mismatch(self, client, allow_everyone):
        """Microsoft's own token carries the serviceUrl without the trailing
        slash the activity has. A comparison that failed on that would refuse
        every genuine activity, which is a self-inflicted outage."""
        token = _token(service_url=SERVICE_URL.rstrip("/"))
        assert _post(client, token=token).status_code == 200


class TestSizeAndRate:
    def test_an_activity_over_the_cap_is_413(self, client):
        big = _activity(text="x" * (router_module.MAX_ACTIVITY_BYTES + 1024))
        response = _post(client, activity=big)
        assert response.status_code == 413

    def test_the_cap_is_applied_before_the_body_is_parsed_or_authenticated(self, client, recorded):
        """A cap that ran after ``await request.json()`` would have buffered the
        whole thing first, which is the only part that costs anything."""
        big = _activity(text="y" * (router_module.MAX_ACTIVITY_BYTES + 1024))
        assert _post(client, activity=big, token="not-even-a-token").status_code == 413
        assert recorded == []

    def test_a_flood_is_throttled_with_429(self, client, allow_everyone):
        codes = [_post(client).status_code for _ in range(router_module.RATE_LIMIT_PER_MINUTE + 5)]
        assert 429 in codes
        assert codes[0] == 200, "the first request was throttled"

    def test_throttling_does_not_run_the_agent(self, client, allow_everyone):
        for _ in range(router_module.RATE_LIMIT_PER_MINUTE + 5):
            _post(client)
        _drain(client)
        assert len(client.runner.calls) <= router_module.RATE_LIMIT_PER_MINUTE


class TestAValidActivity:
    def test_it_is_acknowledged_immediately(self, client, allow_everyone):
        """Teams abandons a request after about 15 seconds. The run takes
        longer than that routinely, so the ack cannot wait for it."""
        assert _post(client).status_code == 200
        assert client.runner.calls == [], "the run happened inside the request"

    def test_the_conversation_reference_is_recorded(self, client, recorded, allow_everyone):
        _post(client)
        assert len(recorded) == 1
        written = recorded[0]
        assert written["channel"] == "teams"
        assert written["native_id"] == ALICE_AAD
        assert written["conversation_id"] == CONVERSATION
        assert written["service_url"] == SERVICE_URL

    def test_the_directory_object_id_identifies_the_sender_not_the_client_id(
        self, client, recorded, allow_everyone
    ):
        """``from.id`` is per-Teams-client and changes; ``aadObjectId`` is the
        person in the directory. Pairing binds the one that survives."""
        _post(client)
        assert recorded[0]["native_id"] == ALICE_AAD

    def test_a_sender_with_no_directory_id_falls_back_to_the_platform_id(
        self, client, recorded, allow_everyone
    ):
        """A guest or an anonymous meeting participant has no aadObjectId. They
        are still somebody, and refusing them silently would be a channel that
        works for staff and mysteriously not for guests."""
        _post(client, activity=_activity(**{"from": {"id": ALICE_OBJECT_ID, "name": "Alice"}}))
        assert recorded[0]["native_id"] == ALICE_OBJECT_ID

    def test_the_shared_pipeline_runs_exactly_once_with_the_senders_identity(
        self, client, allow_everyone
    ):
        _post(client)
        _drain(client)
        assert len(client.runner.calls) == 1
        call = client.runner.calls[0]
        assert call["message"] == "hello"
        assert call["user_id"] == f"teams:{ALICE_AAD}"

    def test_the_answer_goes_back_over_the_outbound_channel(self, client, sent, allow_everyone):
        _post(client)
        _drain(client)
        assert sent == [(ALICE_AAD, "the answer")]

    def test_the_bots_own_mention_is_stripped_from_the_text(self, client, allow_everyone):
        activity = _activity(
            text="<at>Genus</at> what is the plan",
            entities=[
                {
                    "type": "mention",
                    "text": "<at>Genus</at>",
                    "mentioned": {"id": f"28:{APP_ID}", "name": "Genus"},
                }
            ],
        )
        _post(client, activity=activity)
        _drain(client)
        assert client.runner.calls[0]["message"] == "what is the plan"

    def test_the_bots_own_activities_are_ignored(self, client, recorded):
        """A bot answering its own message is a loop with a bill attached."""
        activity = _activity(**{"from": {"id": f"28:{APP_ID}", "name": "Genus"}})
        assert _post(client, activity=activity).status_code == 200
        _drain(client)
        assert client.runner.calls == []

    def test_an_activity_that_is_not_a_message_runs_nothing(self, client):
        assert _post(client, activity=_activity(type="conversationUpdate")).status_code == 200
        _drain(client)
        assert client.runner.calls == []


class TestAccess:
    def test_an_unknown_sender_in_a_one_to_one_chat_is_sent_a_pairing_code(
        self, client, sent, monkeypatch
    ):
        from robothor.engine.channels import access

        async def _evaluate(*_a: Any, **kw: Any) -> access.AccessDecision:
            assert kw["surface"] == access.DIRECT_SURFACE
            return access.AccessDecision(
                allowed=False, refusal="Share this code with the operator: ABC234"
            )

        monkeypatch.setattr(access, "evaluate", _evaluate)
        _post(client)
        _drain(client)

        assert client.runner.calls == [], "an unpaired stranger drove the agent"
        assert sent and "ABC234" in sent[0][1]

    def test_an_unknown_sender_in_a_channel_learns_nothing_at_all(self, client, sent, monkeypatch):
        """No code is ever minted in a room — anyone in it could carry it to the
        operator — and nothing is sent back either, so a stranger who guessed
        the bot's name does not even learn that somebody is listening."""
        from robothor.engine.channels import access

        seen: dict[str, Any] = {}

        async def _evaluate(*_a: Any, **kw: Any) -> access.AccessDecision:
            seen.update(kw)
            return access.AccessDecision(allowed=False, refusal="")

        monkeypatch.setattr(access, "evaluate", _evaluate)
        _post(
            client,
            activity=_activity(conversation={"id": CONVERSATION, "conversationType": "channel"}),
        )
        _drain(client)

        assert seen["surface"] == access.GROUP_SURFACE
        assert sent == []
        assert client.runner.calls == []

    def test_the_gate_is_asked_about_the_teams_channel_and_its_own_mode(self, client, monkeypatch):
        from robothor.engine.channels import access

        seen: dict[str, Any] = {}

        async def _evaluate(channel: str, native_id: str, **kw: Any) -> access.AccessDecision:
            seen.update({"channel": channel, "native_id": native_id, **kw})
            return access.AccessDecision(allowed=False, refusal="")

        monkeypatch.setattr(access, "evaluate", _evaluate)
        _post(client)
        _drain(client)

        assert seen["channel"] == "teams"
        assert seen["native_id"] == ALICE_AAD


class TestTheKeysAreCachedButNotForever:
    @pytest.mark.asyncio
    async def test_the_published_keys_are_fetched_once(self, _served_keys):
        await jwt_validation.signing_key(KID)
        await jwt_validation.signing_key(KID)
        assert _served_keys["keys"] == 1

    @pytest.mark.asyncio
    async def test_an_unknown_kid_refetches_once_before_giving_up(self, _served_keys):
        """Microsoft rotates these keys. A cache that never refetched would
        refuse every genuine activity from the moment of a rotation until the
        process restarted — an outage with no cause visible anywhere."""
        await jwt_validation.signing_key(KID)  # a warm cache
        with pytest.raises(jwt_validation.TokenRejectedError):
            await jwt_validation.signing_key("a-rotated-in-key")
        assert _served_keys["keys"] == 2

    @pytest.mark.asyncio
    async def test_a_stream_of_unknown_kids_cannot_make_us_hammer_microsoft(self, _served_keys):
        """The other half of the rotation refetch: an unauthenticated caller
        chooses the ``kid``, so a refetch per unknown one is a way to make this
        instance flood Microsoft — and then be rate limited into refusing every
        genuine activity."""
        await jwt_validation.signing_key(KID)
        for index in range(20):
            with pytest.raises(jwt_validation.TokenRejectedError):
                await jwt_validation.signing_key(f"probe-{index}")
        assert _served_keys["keys"] == 2

    @pytest.mark.asyncio
    async def test_the_cache_expires(self, _served_keys, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(jwt_validation, "_clock", lambda: clock["now"])
        await jwt_validation.signing_key(KID)
        clock["now"] += jwt_validation.KEY_CACHE_SECONDS + 1
        await jwt_validation.signing_key(KID)
        assert _served_keys["keys"] == 2
