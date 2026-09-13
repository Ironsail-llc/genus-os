"""``SlackChannel`` — Slack as a delivery target, with proof.

Until now ``delivery.channel: slack`` resolved to the generic sender shim built
around ``SlackBot.slack_send``, which can only send once the *inbound* Socket
Mode bot has started. That is the "declared and inert" trap
``robothor/engine/channels/base.py`` warns about in full: an instance that only
wanted to post a briefing into a channel had to run a socket listener first, and
an operator whose app token was wrong got silence from the outbound half too.

The rule every test here turns on is the receipt rule: **acknowledged is counted
from what Slack returned, never from reaching the next line.** A chunk that
raised is a chunk nobody saw, and a send that partially landed is
``partial:``, not a total failure that throws away the ids of the messages the
operator can actually read.

Every id here is a placeholder (``C0000000000``, ``U0000000000``) and the token
is visibly fake: this is platform code, and a real workspace id is instance
data.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.channels.base import SendReceipt
from robothor.engine.channels.slack import SlackChannel
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus

#: Visibly not a credential. Long enough that a substring search for it in a
#: report or a log line is a real test rather than a coincidence.
FAKE_BOT_TOKEN = "xoxb-test-not-a-real-token"
FAKE_APP_TOKEN = "xapp-test-not-a-real-token"

CHANNEL_ID = "C0000000000"
USER_ID = "U0000000000"
DM_ID = "D0000000000"


class _FakeResponse:
    """Stand-in for ``slack_sdk``'s ``SlackResponse``: a mapping over ``data``."""

    def __init__(self, **data: Any) -> None:
        self.data = {"ok": True, **data}

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class _FakeSlackError(Exception):
    """What ``slack_sdk`` raises: the payload hangs off ``.response``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error", "slack error")))
        self.response = payload


class _FakeClient:
    def __init__(
        self,
        *,
        token: str = FAKE_BOT_TOKEN,
        fail_posts: set[int] | None = None,
        auth: dict[str, Any] | None = None,
        auth_error: Exception | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.token = token
        self.posts: list[dict[str, Any]] = []
        #: Counts ATTEMPTS, not successes: ``fail_posts={2}`` has to mean "the
        #: second chunk", and a counter derived from ``len(self.posts)`` would
        #: renumber chunk 3 as chunk 2 and reject that one too.
        self.attempts = 0
        self.opened: list[str] = []
        self.auth_calls = 0
        self.list_calls = 0
        self.connections = 0
        self._fail_posts = fail_posts or set()
        self._auth = auth or {"team": "Example Workspace", "user_id": "U0000000001"}
        self._auth_error = auth_error
        self._list_error = list_error

    async def chat_postMessage(  # noqa: N802 — the Slack SDK's own method name
        self, **kwargs: Any
    ) -> _FakeResponse:
        self.attempts += 1
        index = self.attempts
        if index in self._fail_posts:
            raise _FakeSlackError({"error": "channel_not_found"})
        self.posts.append(kwargs)
        return _FakeResponse(ts=f"1700000000.{index:06d}", channel=kwargs.get("channel"))

    async def conversations_open(self, *, users: str, **_: Any) -> _FakeResponse:
        self.opened.append(users)
        return _FakeResponse(channel={"id": DM_ID})

    async def auth_test(self) -> _FakeResponse:
        self.auth_calls += 1
        if self._auth_error is not None:
            raise self._auth_error
        return _FakeResponse(**self._auth)

    async def conversations_list(self, **_: Any) -> _FakeResponse:
        self.list_calls += 1
        if self._list_error is not None:
            raise self._list_error
        return _FakeResponse(channels=[])

    async def apps_connections_open(self) -> _FakeResponse:
        self.connections += 1
        return _FakeResponse(url="wss://example.invalid/link")


def _channel(client: Any = None, **kwargs: Any) -> SlackChannel:
    channel = SlackChannel(**kwargs)
    if client is not None:
        channel.client_factory = lambda _token: client
    return channel


def _config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "alice",
        "name": "Alice",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": CHANNEL_ID,
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


def _run() -> AgentRun:
    return AgentRun(agent_id="alice", status=RunStatus.COMPLETED)


@pytest.fixture(autouse=True)
def _configured(monkeypatch, tmp_path):
    """A configured instance by default; the unconfigured tests undo it.

    The workspace is pinned to a scratch directory and the settings cache is
    dropped, so no test in this module reads THIS box's configuration.
    ``test_verify_without_a_target_cannot_prove_a_post`` is why: it asserts that
    ``verify()`` with no target cannot post, and
    ``SlackChannel.verify_target()`` reads real settings. Any instance that
    followed ``docs/channels/slack.md`` and ran ``genus channel add slack
    --verify-target …`` has that value in ``<workspace>/.robothor/config.yaml``,
    and the test failed on it. CI was green only because CI has no such file —
    the test passed for the wrong reason.
    """
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_SLACK_BOT_TOKEN", FAKE_BOT_TOKEN)
    monkeypatch.setenv("ROBOTHOR_SLACK_APP_TOKEN", FAKE_APP_TOKEN)
    monkeypatch.delenv("ROBOTHOR_SLACK_VERIFY_TARGET", raising=False)
    reset_settings()
    yield
    reset_settings()


def _long_body(chunks: int) -> str:
    from robothor.engine.channels.slack import MAX_SLACK_LENGTH

    return "x" * (MAX_SLACK_LENGTH * (chunks - 1) + 100)


class TestWhatTheChannelCanProve:
    @pytest.mark.asyncio
    async def test_every_chunk_acknowledged_is_delivered(self):
        client = _FakeClient()
        receipt = await _channel(client).send(
            CHANNEL_ID, _long_body(3), config=_config(), run=_run()
        )

        assert (receipt.acknowledged, receipt.expected) == (3, 3)
        assert receipt.complete is True
        assert receipt.platform_ids == [
            "1700000000.000001",
            "1700000000.000002",
            "1700000000.000003",
        ]

    @pytest.mark.asyncio
    async def test_a_mid_chunk_failure_is_partial_not_total(self):
        """Losing the ids of the two that landed is how a reply stops resolving."""
        client = _FakeClient(fail_posts={2})
        receipt = await _channel(client).send(
            CHANNEL_ID, _long_body(3), config=_config(), run=_run()
        )

        assert (receipt.acknowledged, receipt.expected) == (2, 3)
        assert receipt.complete is False
        assert len(receipt.platform_ids) == 2

    @pytest.mark.asyncio
    async def test_no_chunk_landing_acknowledges_nothing(self):
        from robothor.engine.delivery import apply_receipt

        client = _FakeClient(fail_posts={1, 2, 3})
        run = _run()
        receipt = await _channel(client).send(CHANNEL_ID, _long_body(3), config=_config(), run=run)

        assert receipt.acknowledged == 0
        assert apply_receipt(run, "slack", receipt) is False
        assert run.delivery_status == "failed:slack_send"
        assert run.delivered_at is None

    @pytest.mark.asyncio
    async def test_client_construction_raising_is_a_receipt_not_an_exception(self):
        """``slack_sdk`` may not be installed at all. A missing optional
        dependency is a failed delivery, not a traceback out of ``deliver()``."""

        def _boom(_token: str) -> Any:
            raise ImportError("No module named 'slack_sdk'")

        channel = SlackChannel()
        channel.client_factory = _boom
        receipt = await channel.send(CHANNEL_ID, "hello", config=_config(), run=_run())

        assert receipt.acknowledged == 0
        assert (receipt.status or "").startswith("failed:slack_")

    @pytest.mark.asyncio
    async def test_the_agent_name_heads_the_body(self):
        """The shim prefixed the display name plain; dropping it on the way to a
        first-class channel would silently change what every Slack briefing
        looks like."""
        client = _FakeClient()
        receipt = await _channel(client).send(CHANNEL_ID, "hello", config=_config(), run=_run())

        assert client.posts[0]["text"].startswith("Alice\n\n")
        assert receipt.body.startswith("Alice\n\n")


class TestThreads:
    @pytest.mark.asyncio
    async def test_thread_ts_is_passed_through(self):
        client = _FakeClient()
        await _channel(client).send(
            CHANNEL_ID, _long_body(2), config=_config(), run=_run(), thread="1700000000.000009"
        )

        assert [post.get("thread_ts") for post in client.posts] == [
            "1700000000.000009",
            "1700000000.000009",
        ]

    @pytest.mark.asyncio
    async def test_without_a_thread_the_tail_hangs_off_the_first_chunk(self):
        """Three loose 4,000-character posts in a busy channel is not a briefing."""
        client = _FakeClient()
        await _channel(client).send(CHANNEL_ID, _long_body(3), config=_config(), run=_run())

        assert client.posts[0].get("thread_ts") is None
        assert [post.get("thread_ts") for post in client.posts[1:]] == [
            "1700000000.000001",
            "1700000000.000001",
        ]


class TestTargets:
    @pytest.mark.asyncio
    async def test_unexpanded_target_is_refused(self):
        client = _FakeClient()
        receipt = await _channel(client).send(
            "${SLACK_CHANNEL}", "hello", config=_config(), run=_run()
        )

        assert receipt.status == "failed:slack_unexpanded_target"
        assert client.posts == [], "the briefing went out to a literal ${SLACK_CHANNEL}"

    @pytest.mark.asyncio
    async def test_an_empty_target_is_refused(self):
        client = _FakeClient()
        receipt = await _channel(client).send("", "hello", config=_config(), run=_run())

        assert receipt.status == "failed:slack_no_target"
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_user_id_target_opens_a_dm(self):
        client = _FakeClient()
        receipt = await _channel(client).send(USER_ID, "hello", config=_config(), run=_run())

        assert client.opened == [USER_ID]
        assert client.posts[0]["channel"] == DM_ID
        assert receipt.complete is True

    @pytest.mark.asyncio
    async def test_unresolvable_target_is_refused(self):
        """``#general`` is a name, not an id, and resolving one costs a
        ``conversations.list`` walk on every send."""
        client = _FakeClient()
        receipt = await _channel(client).send("#general", "hello", config=_config(), run=_run())

        assert receipt.status == "failed:slack_unresolved_target"
        assert client.posts == []


class TestAnUnconfiguredInstance:
    @pytest.mark.asyncio
    async def test_unconfigured_send_is_failed_not_configured(self, monkeypatch):
        """The channel is registered whether or not Slack is set up, so the
        honest answer to a manifest naming it has to be loud."""
        monkeypatch.delenv("ROBOTHOR_SLACK_BOT_TOKEN", raising=False)
        client = _FakeClient()
        receipt = await _channel(client).send(CHANNEL_ID, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:slack_not_configured"
        assert receipt.acknowledged == 0
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_health_says_unconfigured_without_calling_slack(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SLACK_BOT_TOKEN", raising=False)
        client = _FakeClient()
        report = await _channel(client).health()

        assert report["configured"] is False
        assert client.auth_calls == 0


class TestItSplitsWithTheSameFunctionTheBotDoes:
    def test_the_channel_and_the_bot_split_with_the_same_function(self):
        """Two splitters that disagree by one turn a truncated briefing into a
        delivered one — the drift ``chunking.split_message`` was extracted to
        prevent, one surface further out."""
        from robothor.engine import slack as slack_bot
        from robothor.engine.channels import slack as slack_channel

        assert slack_channel.split_message is slack_bot.split_message
        assert slack_channel.MAX_SLACK_LENGTH is slack_bot.MAX_SLACK_LENGTH


class TestTheOptionalSlots:
    @pytest.mark.asyncio
    async def test_ask_and_resolve_identity_raise(self):
        channel = SlackChannel()
        with pytest.raises(NotImplementedError):
            await channel.ask("Approve?", ["yes", "no"])
        with pytest.raises(NotImplementedError):
            await channel.resolve_identity(USER_ID)

    @pytest.mark.asyncio
    async def test_ask_accepts_the_whole_protocol_signature(self):
        """A slot that only accepts the two positional arguments is one the
        real callers cannot reach: both ``ask_user`` and
        ``PermissionEscalationManager`` pass ``timeout`` and ``target``, and a
        TypeError there would be caught by their broad handlers and reported as
        "the channel broke" rather than "Slack cannot ask yet"."""
        with pytest.raises(NotImplementedError):
            await SlackChannel().ask("Approve?", ["yes", "no"], timeout=5.0, target=CHANNEL_ID)

    @pytest.mark.asyncio
    async def test_a_slack_run_still_gets_a_durable_question(self):
        """The refusal is not a dead end. ``ask_user`` catches it, records the
        question with no channel, and the operator answers it from the Helm."""
        from robothor.engine.tools.handlers.ask_user import _ask_channel

        answer, delivered, _waited = await _ask_channel(
            SlackChannel(), "Approve?", [], 5.0, CHANNEL_ID, USER_ID
        )
        assert answer is None
        # `delivered` False is the whole point: the run is told nobody could be
        # asked, not that it waited five seconds for a silent operator.
        assert delivered is False


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_reports_auth_test(self):
        client = _FakeClient()
        report = await _channel(client).health()

        assert report["channel"] == "slack"
        assert report["configured"] is True
        assert report["ok"] is True
        assert report["team"] == "Example Workspace"
        assert report["bot_user_id"] == "U0000000001"
        assert FAKE_BOT_TOKEN not in str(report)

    @pytest.mark.asyncio
    async def test_a_rejected_token_is_not_ok_and_does_not_raise(self):
        client = _FakeClient(auth_error=_FakeSlackError({"error": "invalid_auth"}))
        report = await _channel(client).health()

        assert report["ok"] is False
        assert "invalid_auth" in report.get("error", "")


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_reports_each_step(self):
        client = _FakeClient()
        steps = await _channel(client).verify(target=CHANNEL_ID)

        names = [step for step, _ok, _detail in steps]
        assert names == [
            "auth.test",
            "conversations.list",
            "chat.postMessage",
            "apps.connections.open",
        ]
        assert all(ok for _step, ok, _detail in steps)
        assert client.auth_calls == 1, "verify authenticated more than once"
        assert client.list_calls == 1
        assert client.connections == 1

        # m6: this report is pasted into bug reports. The workspace name and the
        # bot id are the operator's own data; `health()` reports them because
        # the operator asked this instance about itself, `verify` does not.
        report = repr(steps)
        assert "Example Workspace" not in report
        assert "U0000000001" not in report

    @pytest.mark.asyncio
    async def test_a_post_that_returns_no_ts_is_not_a_pass(self):
        """ "It did not raise" is the one thing that is never evidence here."""

        class _Silent(_FakeClient):
            async def chat_postMessage(self, **kwargs: Any) -> _FakeResponse:  # noqa: N802
                self.posts.append(kwargs)
                return _FakeResponse(ts=None)

        steps = await _channel(_Silent()).verify(target=CHANNEL_ID)
        assert {step: ok for step, ok, _ in steps}["chat.postMessage"] is False

    @pytest.mark.asyncio
    async def test_verify_names_the_missing_scope_not_the_token(self):
        error = _FakeSlackError(
            {
                "error": "missing_scope",
                "needed": "channels:read",
                "provided": "chat:write",
            }
        )
        client = _FakeClient(list_error=error)
        steps = await _channel(client).verify(target=CHANNEL_ID)

        by_step = {step: (ok, detail) for step, ok, detail in steps}
        ok, detail = by_step["conversations.list"]
        assert ok is False
        assert "channels:read" in detail
        report = repr(steps)
        assert FAKE_BOT_TOKEN not in report
        assert FAKE_APP_TOKEN not in report

    @pytest.mark.asyncio
    async def test_verify_without_a_target_cannot_prove_a_post(self):
        client = _FakeClient()
        steps = await _channel(client).verify()

        by_step = {step: ok for step, ok, _detail in steps}
        assert by_step["chat.postMessage"] is False
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_verify_uses_the_configured_target_when_given_none(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_SLACK_VERIFY_TARGET", CHANNEL_ID)
        from robothor.settings import reset_settings

        reset_settings()
        client = _FakeClient()
        try:
            steps = await _channel(client).verify()
        finally:
            reset_settings()

        assert client.posts and client.posts[0]["channel"] == CHANNEL_ID
        assert {step: ok for step, ok, _ in steps}["chat.postMessage"] is True


class TestTheRegistry:
    def test_slack_is_a_builtin_channel(self):
        from robothor.engine.channels import BUILTIN_CHANNELS, get_channel, reset_channels

        reset_channels()
        try:
            assert "slack" in BUILTIN_CHANNELS
            channel = get_channel("slack")
            assert isinstance(channel, SlackChannel)
        finally:
            reset_channels()

    def test_the_sender_shim_no_longer_shadows_it(self):
        """``SlackBot.start`` registers a ``slack`` platform sender at bot start.
        Before this it REPLACED the channel with a shim that could only send
        through the inbound bot; a built-in name is exempt from that."""
        from robothor.engine.channels import get_channel, reset_channels
        from robothor.engine.delivery import register_platform_sender

        reset_channels()
        try:

            async def _sender(target: str, text: str, **_: Any) -> list[Any]:
                return []

            register_platform_sender("slack", _sender, chunk_size=4000)
            assert isinstance(get_channel("slack"), SlackChannel)
        finally:
            reset_channels()

    @pytest.mark.asyncio
    async def test_a_manifest_naming_slack_reaches_the_channel(self):
        """``delivery.channel: slack`` end to end through ``deliver()``."""
        from robothor.engine import delivery
        from robothor.engine.channels import get_channel, reset_channels

        reset_channels()
        try:
            channel = get_channel("slack")
            assert isinstance(channel, SlackChannel)
            client = _FakeClient()
            channel.client_factory = lambda _token: client

            config = _config(delivery_channel="slack")
            run = _run()
            run.output_text = "the morning briefing"
            assert await delivery.deliver(config, run) is True
        finally:
            reset_channels()

        assert run.delivery_status == "delivered"
        assert run.delivery_channel == "slack"
        assert client.posts[0]["channel"] == CHANNEL_ID

    @pytest.mark.asyncio
    async def test_the_channel_satisfies_the_protocol(self):
        from robothor.engine.channels import Channel

        assert isinstance(SlackChannel(), Channel)

    @pytest.mark.asyncio
    async def test_send_returns_a_receipt_for_every_refusal(self):
        """Every early return is a :class:`SendReceipt`, never None and never a
        raise: ``deliver()`` reads the receipt, and a caller that has to catch
        an exception to notice non-delivery will eventually forget to."""
        client = _FakeClient()
        channel = _channel(client)
        for target in ("", "${X}", "#general", CHANNEL_ID):
            receipt = await channel.send(target, "hello", config=_config(), run=_run())
            assert isinstance(receipt, SendReceipt)


class TestAFailureStatusIsAStableToken:
    """``failed:slack_client: <whatever the SDK said>`` was two defects.

    It embedded free-form text, so nothing could match it beyond
    ``startswith("failed:")`` while every other ``delivery_status`` is a token a
    query or an alert rule matches exactly. And the text came from the raw
    exception: a token that reached the SDK malformed — an embedded newline
    surviving the vault — makes it raise ``ValueError: Invalid header value
    b'Bearer xoxb-…'``, which went into ``agent_runs`` verbatim and onto the
    dashboard.
    """

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_a_closed_reason(self):
        def _boom(_token: str) -> Any:
            raise ImportError("No module named 'slack_sdk'")

        channel = SlackChannel()
        channel.client_factory = _boom
        receipt = await channel.send(CHANNEL_ID, "hello", config=_config(), run=_run())

        assert receipt.status == "failed:slack_client:transport"

    @pytest.mark.asyncio
    async def test_a_dm_that_cannot_be_opened_is_its_own_reason(self):
        class _RefusesToOpen(_FakeClient):
            async def conversations_open(self, *, users: str, **_: Any) -> _FakeResponse:
                raise _FakeSlackError({"error": "user_not_found"})

        receipt = await _channel(_RefusesToOpen()).send(
            USER_ID, "hello", config=_config(), run=_run()
        )

        assert receipt.status == "failed:slack_client:dm_open"

    @pytest.mark.asyncio
    async def test_no_sdk_error_text_reaches_the_status_or_the_log(self, caplog):
        """A malformed credential makes the SDK put the token in its own message."""

        def _leaks(_token: str) -> Any:
            raise ValueError(f"Invalid header value b'Bearer {FAKE_BOT_TOKEN}'")

        channel = SlackChannel()
        channel.client_factory = _leaks
        with caplog.at_level("DEBUG"):
            receipt = await channel.send(CHANNEL_ID, "hello", config=_config(), run=_run())

        assert FAKE_BOT_TOKEN not in (receipt.status or "")
        assert FAKE_BOT_TOKEN not in caplog.text
        assert "Bearer" not in caplog.text


class TestTheScopeProbeIsShared:
    """One probe and one wording, or the CLI reports a missing scope while the
    Health panel shows green for the same app."""

    @pytest.mark.asyncio
    async def test_scope_probe_returns_none_when_the_scopes_answer(self):
        assert await _channel(_FakeClient()).scope_probe() is None

    @pytest.mark.asyncio
    async def test_scope_probe_names_the_needed_scope_and_no_token(self):
        error = _FakeSlackError(
            {"error": "missing_scope", "needed": "channels:read", "provided": "chat:write"}
        )
        detail = await _channel(_FakeClient(list_error=error)).scope_probe()

        assert detail is not None
        assert "channels:read" in detail
        assert FAKE_BOT_TOKEN not in detail

    @pytest.mark.asyncio
    async def test_an_unconfigured_instance_gets_a_sentence_not_a_raise(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_SLACK_BOT_TOKEN", raising=False)
        detail = await _channel(_FakeClient()).scope_probe()
        assert detail is not None and "ROBOTHOR_SLACK_BOT_TOKEN" in detail
