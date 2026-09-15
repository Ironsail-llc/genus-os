"""Outbound Teams: the token, the send, and what may never appear in either.

Nothing here touches Microsoft. Every HTTP call goes through a fixture
transport, and the *recorded* shapes are what Microsoft documents: a
client-credentials token response, an activity response carrying the platform's
own message id, and the two failures an operator actually hits — a rejected
secret and an expired token.

The invariants under test are the platform's, not Teams':

* **a receipt is derived from what the platform returned**, never from reaching
  the next line (``robothor/engine/CLAUDE.md``);
* **a missing conversation reference is loud**. There is no fallback
  conversation and there must never be one: posting a briefing into whichever
  chat the box happens to know about is the failure
  ``failed:telegram_unexpanded_chat_id`` was invented to stop;
* **no credential, and no fragment of one, reaches health, verify, or a log.**
"""

from __future__ import annotations

import time

import httpx
import pytest
from genus_teams import channel as channel_module
from genus_teams import credentials as credentials_module
from genus_teams import tokens as tokens_module
from genus_teams.channel import TeamsChannel

APP_ID = "00000000-0000-0000-0000-00000000aaaa"
APP_PASSWORD = "not-a-real-client-secret-1234567890"
DIRECTORY_TENANT = "00000000-0000-0000-0000-00000000bbbb"
TENANT = "00000000-0000-0000-0000-000000000000"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
CONVERSATION = "19:meeting_abc@thread.v2"
ALICE = "29:alice-object-id"

#: What Entra returns for a client-credentials grant, trimmed to the fields any
#: client reads. The token value is visibly fake on purpose.
TOKEN_RESPONSE = {
    "token_type": "Bearer",
    "expires_in": 3599,
    "access_token": "not-a-real-bot-framework-token",
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Credentials resolved without a vault, an environment, or a settings file."""
    creds = credentials_module.TeamsCredentials(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        directory_tenant_id=DIRECTORY_TENANT,
    )
    monkeypatch.setattr(credentials_module, "teams_credentials", lambda **_kw: creds)
    monkeypatch.setattr(channel_module, "teams_credentials", lambda **_kw: creds)
    monkeypatch.setattr(tokens_module, "teams_credentials", lambda **_kw: creds)
    return creds


@pytest.fixture
def unconfigured(monkeypatch):
    empty = credentials_module.TeamsCredentials()
    monkeypatch.setattr(credentials_module, "teams_credentials", lambda **_kw: empty)
    monkeypatch.setattr(channel_module, "teams_credentials", lambda **_kw: empty)
    monkeypatch.setattr(tokens_module, "teams_credentials", lambda **_kw: empty)
    return empty


class _Recorder:
    """A recorded-fixture transport: it answers, and it remembers."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def _ok_handler(activity_id: str = "1700000000001"):
    def _handle(request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return httpx.Response(200, json={"id": activity_id})

    return _handle


@pytest.fixture
def transport(monkeypatch):
    def _install(handler) -> _Recorder:
        recorder = _Recorder(handler)
        monkeypatch.setattr(tokens_module, "build_client", recorder.client)
        monkeypatch.setattr(channel_module, "build_client", recorder.client)
        return recorder

    return _install


@pytest.fixture
def known_conversation(monkeypatch):
    """One recorded conversation reference, without a database."""
    from robothor.engine.channels.conversations import ConversationRef

    ref = ConversationRef(
        channel="teams",
        native_id=ALICE,
        conversation_id=CONVERSATION,
        service_url=SERVICE_URL,
    )
    monkeypatch.setattr(
        channel_module.conversations, "reference_for_target", lambda *_a, **_kw: ref
    )
    return ref


@pytest.fixture
def no_conversation(monkeypatch):
    monkeypatch.setattr(
        channel_module.conversations, "reference_for_target", lambda *_a, **_kw: None
    )


class TestTheToken:
    @pytest.mark.asyncio
    async def test_a_token_is_fetched_from_the_bot_framework_scope(self, transport):
        recorder = transport(_ok_handler())
        source = tokens_module.TokenSource()
        assert await source.token() == TOKEN_RESPONSE["access_token"]

        request = recorder.requests[0]
        assert "login.microsoftonline.com" in str(request.url)
        body = request.content.decode()
        assert "grant_type=client_credentials" in body
        assert "https%3A%2F%2Fapi.botframework.com%2F.default" in body

    @pytest.mark.asyncio
    async def test_a_single_tenant_bot_authenticates_against_its_own_directory(self, transport):
        recorder = transport(_ok_handler())
        await tokens_module.TokenSource().token()
        assert DIRECTORY_TENANT in recorder.urls[0]

    @pytest.mark.asyncio
    async def test_the_token_is_cached_until_it_expires(self, transport, monkeypatch):
        recorder = transport(_ok_handler())
        clock = {"now": 1000.0}
        source = tokens_module.TokenSource(clock=lambda: clock["now"])

        await source.token()
        await source.token()
        assert len(recorder.requests) == 1, "a token was fetched per send"

        # Past expiry (3599s) minus the refresh skew.
        clock["now"] += 3600.0
        await source.token()
        assert len(recorder.requests) == 2

    @pytest.mark.asyncio
    async def test_it_refreshes_before_expiry_rather_than_after(self, transport):
        """A token that is refreshed at the moment it expires is a token that
        has already expired by the time the request lands."""
        recorder = transport(_ok_handler())
        clock = {"now": 1000.0}
        source = tokens_module.TokenSource(clock=lambda: clock["now"])
        await source.token()
        clock["now"] += 3599.0 - (tokens_module.REFRESH_SKEW_SECONDS - 1)
        await source.token()
        assert len(recorder.requests) == 2

    @pytest.mark.asyncio
    async def test_a_rejected_secret_raises_without_carrying_the_secret(self, transport, caplog):
        def _reject(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_description": f"AADSTS7000215: Invalid client secret {APP_PASSWORD}",
                },
            )

        transport(_reject)
        source = tokens_module.TokenSource()
        with caplog.at_level("DEBUG"):
            with pytest.raises(tokens_module.TokenError) as raised:
                await source.token()

        assert APP_PASSWORD not in str(raised.value)
        assert APP_PASSWORD not in caplog.text
        assert "invalid_client" in str(raised.value), "the operator lost the only useful word"

    @pytest.mark.asyncio
    async def test_the_last_attempt_is_reportable_without_the_token(self, transport):
        recorder = transport(_ok_handler())
        source = tokens_module.TokenSource()
        await source.token()
        state = source.last_attempt()
        assert state["ok"] is True
        assert TOKEN_RESPONSE["access_token"] not in repr(state)
        assert recorder.requests


class TestSend:
    @pytest.mark.asyncio
    async def test_a_send_posts_an_activity_and_counts_what_came_back(
        self, transport, known_conversation
    ):
        recorder = transport(_ok_handler("1700000000009"))
        receipt = await TeamsChannel().send(ALICE, "the briefing")

        assert receipt.acknowledged == 1
        assert receipt.expected == 1
        assert receipt.platform_ids == ["1700000000009"]
        assert receipt.complete

        activity = recorder.requests[-1]
        assert str(activity.url) == f"{SERVICE_URL}v3/conversations/{CONVERSATION}/activities"
        assert activity.headers["authorization"] == f"Bearer {TOKEN_RESPONSE['access_token']}"

    @pytest.mark.asyncio
    async def test_a_target_with_no_recorded_reference_fails_loudly(
        self, transport, no_conversation
    ):
        transport(_ok_handler())
        receipt = await TeamsChannel().send("29:a-stranger", "hello")
        assert receipt.acknowledged == 0
        assert receipt.status == "failed:teams_no_conversation_reference"

    @pytest.mark.asyncio
    async def test_an_empty_target_is_refused_before_the_credential_is_even_read(
        self, transport, unconfigured
    ):
        transport(_ok_handler())
        receipt = await TeamsChannel().send("", "hello")
        assert receipt.status == "failed:teams_no_target"

    @pytest.mark.asyncio
    async def test_an_unexpanded_variable_in_the_target_is_its_own_failure(
        self, transport, known_conversation
    ):
        transport(_ok_handler())
        receipt = await TeamsChannel().send("${TEAMS_CHAT}", "hello")
        assert receipt.status == "failed:teams_unexpanded_target"

    @pytest.mark.asyncio
    async def test_an_unconfigured_instance_says_so_rather_than_failing_obscurely(
        self, transport, unconfigured, known_conversation
    ):
        transport(_ok_handler())
        receipt = await TeamsChannel().send(ALICE, "hello")
        assert receipt.status == "failed:teams_not_configured"

    @pytest.mark.asyncio
    async def test_a_body_over_the_message_limit_is_chunked(self, transport, known_conversation):
        recorder = transport(_ok_handler())
        body = "x" * (channel_module.MAX_TEAMS_LENGTH * 2 + 10)
        receipt = await TeamsChannel().send(ALICE, body)

        activities = [r for r in recorder.requests if "activities" in str(r.url)]
        assert len(activities) == receipt.expected >= 3
        assert receipt.acknowledged == receipt.expected

    @pytest.mark.asyncio
    async def test_a_rejected_chunk_is_a_partial_receipt_and_not_a_lost_send(
        self, transport, known_conversation
    ):
        """Losing the ids of the chunks that ARE on the person's screen breaks
        reply mapping for messages they can see."""
        state = {"n": 0}

        def _second_fails(request: httpx.Request) -> httpx.Response:
            if "login.microsoftonline.com" in str(request.url):
                return httpx.Response(200, json=TOKEN_RESPONSE)
            state["n"] += 1
            if state["n"] == 2:
                return httpx.Response(429, json={"error": {"code": "Throttled"}})
            return httpx.Response(200, json={"id": f"17000000000{state['n']}"})

        transport(_second_fails)
        body = "y" * (channel_module.MAX_TEAMS_LENGTH * 2 + 10)
        receipt = await TeamsChannel().send(ALICE, body)

        assert 0 < receipt.acknowledged < receipt.expected
        assert not receipt.complete

    @pytest.mark.asyncio
    async def test_a_send_that_could_not_get_a_token_is_a_receipt_not_an_exception(
        self, transport, known_conversation
    ):
        transport(lambda request: httpx.Response(401, json={"error": "invalid_client"}))
        receipt = await TeamsChannel().send(ALICE, "hello")
        assert receipt.acknowledged == 0
        assert receipt.status == "failed:teams_auth"

    @pytest.mark.asyncio
    async def test_the_agent_name_heads_the_message_as_on_every_other_channel(
        self, transport, known_conversation
    ):
        recorder = transport(_ok_handler())

        class _Config:
            id = "main"
            name = "Genus"

        await TeamsChannel().send(ALICE, "the briefing", config=_Config())
        activity = recorder.requests[-1].read().decode()
        assert "Genus" in activity


class TestHealthAndVerifyTellTheTruthAndNothingElse:
    @pytest.mark.asyncio
    async def test_health_reports_configuration_without_a_fragment_of_a_secret(self, transport):
        transport(_ok_handler())
        report = await TeamsChannel().health()
        assert report["channel"] == "teams"
        assert report["configured"] is True
        assert report["ok"] is True
        blob = repr(report)
        assert APP_PASSWORD not in blob
        assert APP_PASSWORD[:8] not in blob
        assert TOKEN_RESPONSE["access_token"] not in blob

    @pytest.mark.asyncio
    async def test_health_on_an_unconfigured_instance_never_raises(self, unconfigured, transport):
        transport(_ok_handler())
        report = await TeamsChannel().health()
        assert report["configured"] is False
        assert report["ok"] is False

    @pytest.mark.asyncio
    async def test_verify_on_an_unconfigured_instance_is_one_step_named_configuration(
        self, unconfigured
    ):
        from robothor.engine.channels.base import UNCONFIGURED_STEP

        steps = await TeamsChannel().verify()
        assert len(steps) == 1
        assert steps[0][0] == UNCONFIGURED_STEP
        assert steps[0][1] is False

    @pytest.mark.asyncio
    async def test_verify_proves_the_token_and_then_the_reach(self, transport, known_conversation):
        transport(_ok_handler())
        steps = await TeamsChannel().verify(target=ALICE)
        names = [step for step, _ok, _detail in steps]
        assert names[:2] == ["credentials", "oauth2.token"]
        assert "activity" in names[-1]
        assert all(ok for _s, ok, _d in steps), steps

    @pytest.mark.asyncio
    async def test_a_verify_with_no_target_fails_that_step_rather_than_guessing(
        self, transport, no_conversation
    ):
        transport(_ok_handler())
        steps = await TeamsChannel().verify()
        failed = [(s, d) for s, ok, d in steps if not ok]
        assert failed, "a verify with nowhere to post reported success"
        assert "ROBOTHOR_TEAMS_VERIFY_TARGET" in " ".join(d for _s, d in failed)

    @pytest.mark.asyncio
    async def test_no_step_detail_carries_a_credential(self, transport, known_conversation):
        def _leaky(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error_description": f"secret {APP_PASSWORD} rejected"},
            )

        transport(_leaky)
        steps = await TeamsChannel().verify(target=ALICE)
        assert APP_PASSWORD not in repr(steps)


class TestTheProtocol:
    def test_the_channel_satisfies_the_runtime_checkable_protocol(self):
        from robothor.engine.channels.base import Channel

        assert isinstance(TeamsChannel(), Channel)

    def test_it_opens_no_transport_at_import_or_at_start(self, transport):
        """base.py: a channel must open its transport lazily inside send, or it
        ships working tests and delivers nothing."""
        recorder = transport(_ok_handler())
        channel = TeamsChannel()
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(channel.start())
        assert recorder.requests == []

    def test_the_name_is_not_a_builtin(self):
        from robothor.engine.channels.registry import BUILTIN_CHANNELS

        assert TeamsChannel.name == "teams"
        assert TeamsChannel.name not in BUILTIN_CHANNELS


def test_the_module_reads_no_environment_variable_directly():
    """Settings resolve through the accessor, so `genus config` can show them
    and the env-read ratchet keeps counting call sites elsewhere."""
    import ast
    from pathlib import Path

    import genus_teams

    for path in Path(genus_teams.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                raise AssertionError(f"{path.name}:{node.lineno} reads the environment directly")


def test_time_is_not_imported_for_a_wall_clock_deadline():
    """The token cache keys on a MONOTONIC clock: a wall clock that steps
    backwards (NTP, a suspend) would make a live token look expired forever."""
    assert tokens_module.time.monotonic is time.monotonic
