"""One inbound pipeline, so a second receiving channel cannot drift from the first.

``SlackBot._on_message`` was the only inbound path a channel could copy, and
copying it is exactly what a new channel would have done: gate → identity →
session → ``runner.execute`` → reply, with the gate call in the middle. Every
line of that is a decision (which mode, which surface, who the run is attributed
to, what an empty output says), and a second copy is a second place for one of
them to be wrong — quietly, because both copies would have passing tests.

So the decisions live here, once, and the channels supply only what is genuinely
theirs: how a message arrives and how a reply goes back out. Slack is built on
it in this commit; ``genus-teams`` is built on it as a plugin, which is the real
proof that the seam is a seam and not a Slack-shaped hole.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.engine.channels import inbound
from robothor.engine.channels.access import AccessDecision
from robothor.engine.models import TriggerType
from robothor.identity import IdentityContext

TENANT = "00000000-0000-0000-0000-000000000000"
ALICE = "29:alice-object-id"


class _Run:
    def __init__(self, output_text: str = "the answer") -> None:
        self.output_text = output_text
        self.id = "run-1"


class _Runner:
    def __init__(self, run: Any = None, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._run = run if run is not None else _Run()
        self._raises = raises

    async def execute(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._raises is not None:
            raise self._raises
        return self._run


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    """The shared session store talks to the database; the pipeline's use of it
    is one line and is not what these tests are about."""

    class _Session:
        history: list[Any] = []

    monkeypatch.setattr(inbound, "get_shared_session", lambda key: _Session())


def _allow(identity: IdentityContext | None = None):
    async def _evaluate(*_a: Any, **_kw: Any) -> AccessDecision:
        return AccessDecision(allowed=True, identity=identity)

    return _evaluate


def _refuse(refusal: str = ""):
    async def _evaluate(*_a: Any, **_kw: Any) -> AccessDecision:
        return AccessDecision(allowed=False, refusal=refusal)

    return _evaluate


async def _handle(monkeypatch, evaluate, runner, **kw: Any) -> inbound.InboundResult:
    monkeypatch.setattr(inbound.access, "evaluate", evaluate)
    return await inbound.handle_message(
        channel=kw.pop("channel", "teams"),
        native_id=kw.pop("native_id", ALICE),
        text=kw.pop("text", "hello"),
        runner=runner,
        tenant_id=kw.pop("tenant_id", TENANT),
        session_key=kw.pop("session_key", "agent:main:teams:19:x"),
        trigger_type=kw.pop("trigger_type", TriggerType.CHANNEL),
        **kw,
    )


class TestTheGateRunsFirst:
    @pytest.mark.asyncio
    async def test_a_refused_sender_never_reaches_the_runner(self, monkeypatch):
        runner = _Runner()
        result = await _handle(monkeypatch, _refuse("here is a code: ABC234"), runner)
        assert runner.calls == [], "a refused message became a run"
        assert result.ran is False
        assert result.reply == "here is a code: ABC234"

    @pytest.mark.asyncio
    async def test_an_empty_refusal_means_send_nothing(self, monkeypatch):
        """The gate answers ``allowed=False, refusal=""`` for an unknown sender
        on a group surface. A channel that invented a message for that would
        tell a stranger somebody is listening."""
        result = await _handle(monkeypatch, _refuse(""), _Runner())
        assert result.reply == ""
        assert result.ran is False

    @pytest.mark.asyncio
    async def test_the_gate_is_told_the_surface_and_the_mode_the_channel_resolved(
        self, monkeypatch
    ):
        seen: dict[str, Any] = {}

        async def _evaluate(channel: str, native_id: str, **kw: Any) -> AccessDecision:
            seen.update({"channel": channel, "native_id": native_id, **kw})
            return AccessDecision(allowed=True)

        await _handle(monkeypatch, _evaluate, _Runner(), surface="group", mode="allowlist")
        assert seen["channel"] == "teams"
        assert seen["native_id"] == ALICE
        assert seen["surface"] == "group"
        assert seen["mode"] == "allowlist"
        assert seen["tenant_id"] == TENANT


class TestTheRun:
    @pytest.mark.asyncio
    async def test_an_allowed_message_runs_exactly_once(self, monkeypatch):
        runner = _Runner()
        result = await _handle(monkeypatch, _allow(), runner)
        assert len(runner.calls) == 1
        assert runner.calls[0]["message"] == "hello"
        assert runner.calls[0]["agent_id"] == "main"
        assert runner.calls[0]["trigger_type"] == TriggerType.CHANNEL
        assert result.ran is True
        assert result.reply == "the answer"

    @pytest.mark.asyncio
    async def test_a_paired_sender_runs_as_the_user_the_operator_bound(self, monkeypatch):
        identity = IdentityContext(
            tenant_id=TENANT,
            channel="teams",
            identifier=ALICE,
            verified=True,
            tenant_user_id="alice",
            role="member",
        )
        runner = _Runner()
        await _handle(monkeypatch, _allow(identity), runner)
        assert runner.calls[0]["user_id"] == "alice"
        assert runner.calls[0]["user_role"] == "member"
        assert runner.calls[0]["identity"] is identity

    @pytest.mark.asyncio
    async def test_an_unpaired_sender_carries_a_synthetic_id_naming_its_channel(self, monkeypatch):
        """Only ``open`` and ``allowlist`` mode reach here unpaired. The id is
        explicit rather than fabricated: an authorization decision about a
        string that matches no row anywhere is the bug this names."""
        runner = _Runner()
        await _handle(monkeypatch, _allow(None), runner)
        assert runner.calls[0]["user_id"] == f"teams:{ALICE}"
        assert runner.calls[0]["user_role"] == "user"

    @pytest.mark.asyncio
    async def test_the_tenant_of_a_bound_identity_wins_over_the_channel_default(self, monkeypatch):
        identity = IdentityContext(
            tenant_id="tenant-b",
            channel="teams",
            identifier=ALICE,
            verified=True,
            tenant_user_id="alice",
            role="viewer",
        )
        runner = _Runner()
        await _handle(monkeypatch, _allow(identity), runner)
        assert runner.calls[0]["tenant_id"] == "tenant-b"


class TestWhatComesBack:
    @pytest.mark.asyncio
    async def test_a_run_with_no_output_says_so_rather_than_sending_nothing(self, monkeypatch):
        runner = _Runner(run=_Run(output_text=""))
        result = await _handle(monkeypatch, _allow(), runner)
        assert result.ran is True
        assert result.reply == inbound.NO_OUTPUT_REPLY

    @pytest.mark.asyncio
    async def test_a_runner_that_raises_is_an_apology_and_not_an_exception(self, monkeypatch):
        """A channel handler that propagated would 500 a webhook, and the
        platform would retry the same message into the same failure."""
        runner = _Runner(raises=RuntimeError("the model is down"))
        result = await _handle(monkeypatch, _allow(), runner)
        assert result.reply == inbound.FAILED_REPLY
        assert result.ran is False
        assert result.failed is True

    @pytest.mark.asyncio
    async def test_the_apology_carries_no_detail_of_the_failure(self, monkeypatch):
        runner = _Runner(raises=RuntimeError("psycopg2: password authentication failed"))
        result = await _handle(monkeypatch, _allow(), runner)
        assert "psycopg2" not in result.reply
        assert "password" not in result.reply


class _GatedRunner:
    """A runner whose run for a given message waits until the test lets it go,
    so a test can hold one conversation mid-run and watch what else moves."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.finished: list[str] = []
        self._fail = fail

    def hold(self, message: str) -> asyncio.Event:
        self.gates[message] = asyncio.Event()
        return self.gates[message]

    async def execute(self, **kw: Any) -> Any:
        self.calls.append(kw)
        gate = self.gates.get(kw["message"])
        if gate is not None:
            await gate.wait()
        self.finished.append(kw["message"])
        if self._fail:
            raise RuntimeError("the model is down")
        return _Run(output_text=f"re: {kw['message']}")


class _Sessions:
    """One real session object per key, so what one message writes the next
    one on the same key can read."""

    def __init__(self) -> None:
        self.by_key: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        from robothor.engine.chat_history import ChatHistory

        if key not in self.by_key:

            class _Session:
                history = ChatHistory()

            self.by_key[key] = _Session()
        return self.by_key[key]


@pytest.fixture
def sessions(monkeypatch) -> _Sessions:
    store = _Sessions()
    monkeypatch.setattr(inbound, "get_shared_session", store.get)
    return store


@pytest.fixture
def fleet_cap(monkeypatch):
    """Set the fleet-wide cap through the environment, the way an operator would."""
    from robothor.settings import reset_settings

    def _set(n: int) -> None:
        monkeypatch.setenv("ROBOTHOR_CHANNEL_MAX_CONCURRENT_RUNS", str(n))
        reset_settings()

    yield _set
    monkeypatch.delenv("ROBOTHOR_CHANNEL_MAX_CONCURRENT_RUNS", raising=False)
    reset_settings()


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout)


async def _settle() -> None:
    """Give every runnable task many turns of the loop."""
    for _ in range(50):
        await asyncio.sleep(0)


class TestConversationsRunConcurrentlyButInOrder:
    """A public chat channel hands this pipeline many visitors at once. Two
    visitors must never wait on each other; one visitor's two messages must
    never race each other's history."""

    @pytest.mark.asyncio
    async def test_a_second_conversation_completes_while_the_first_is_held(
        self, monkeypatch, sessions
    ):
        runner = _GatedRunner()
        gate = runner.hold("first")
        first = asyncio.create_task(
            _handle(monkeypatch, _allow(), runner, text="first", session_key="visitor:a")
        )
        await _until(lambda: len(runner.calls) == 1)

        second = await asyncio.wait_for(
            _handle(monkeypatch, _allow(), runner, text="second", session_key="visitor:b"), 2.0
        )
        assert second.reply == "re: second"
        assert not first.done(), "the held conversation finished early"

        gate.set()
        assert (await first).reply == "re: first"

    @pytest.mark.asyncio
    async def test_a_second_message_in_one_conversation_waits_for_the_first(
        self, monkeypatch, sessions
    ):
        runner = _GatedRunner()
        gate = runner.hold("first")
        first = asyncio.create_task(
            _handle(monkeypatch, _allow(), runner, text="first", session_key="visitor:a")
        )
        await _until(lambda: len(runner.calls) == 1)
        second = asyncio.create_task(
            _handle(monkeypatch, _allow(), runner, text="second", session_key="visitor:a")
        )
        await _settle()
        assert len(runner.calls) == 1, "the second message ran while the first was mid-run"

        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2.0)
        assert runner.finished == ["first", "second"]
        # And it ran with the first exchange already in its history.
        history = runner.calls[1]["conversation_history"]
        assert {"role": "user", "content": "first"} in history
        assert {"role": "assistant", "content": "re: first"} in history

    @pytest.mark.asyncio
    async def test_an_idle_conversation_leaves_no_lock_behind(self, monkeypatch, sessions):
        """Anonymous visitors are unbounded; a lock kept per visitor forever is
        a leak with a slow fuse."""
        runner = _GatedRunner()
        for n in range(5):
            await _handle(monkeypatch, _allow(), runner, session_key=f"visitor:{n}")
        assert inbound._session_locks == {}


class TestTheTurnIsRemembered:
    @pytest.mark.asyncio
    async def test_a_successful_run_appends_exactly_one_turn(self, monkeypatch, sessions):
        await _handle(monkeypatch, _allow(), _GatedRunner(), text="hi", session_key="visitor:a")
        assert list(sessions.get("visitor:a").history) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "re: hi"},
        ]

    @pytest.mark.asyncio
    async def test_record_turn_false_appends_nothing(self, monkeypatch, sessions):
        await _handle(
            monkeypatch,
            _allow(),
            _GatedRunner(),
            text="hi",
            session_key="visitor:a",
            record_turn=False,
        )
        assert list(sessions.get("visitor:a").history) == []

    @pytest.mark.asyncio
    async def test_a_failed_run_appends_nothing(self, monkeypatch, sessions):
        result = await _handle(
            monkeypatch, _allow(), _GatedRunner(fail=True), text="hi", session_key="visitor:a"
        )
        assert result.failed is True
        assert list(sessions.get("visitor:a").history) == []

    @pytest.mark.asyncio
    async def test_a_refused_message_appends_nothing(self, monkeypatch, sessions):
        await _handle(
            monkeypatch, _refuse("no"), _GatedRunner(), text="hi", session_key="visitor:a"
        )
        assert list(sessions.get("visitor:a").history) == []

    @pytest.mark.asyncio
    async def test_history_limit_hands_the_run_only_the_last_turns(self, monkeypatch, sessions):
        session = sessions.get("visitor:a")
        for n in range(5):
            session.history.append({"role": "user", "content": f"q{n}"})
            session.history.append({"role": "assistant", "content": f"a{n}"})
        runner = _GatedRunner()
        await _handle(
            monkeypatch, _allow(), runner, text="now", session_key="visitor:a", history_limit=2
        )
        assert runner.calls[0]["conversation_history"] == [
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "q4"},
            {"role": "assistant", "content": "a4"},
        ]

    @pytest.mark.asyncio
    async def test_without_history_limit_the_run_gets_the_whole_history(
        self, monkeypatch, sessions
    ):
        session = sessions.get("visitor:a")
        for n in range(5):
            session.history.append({"role": "user", "content": f"q{n}"})
            session.history.append({"role": "assistant", "content": f"a{n}"})
        runner = _GatedRunner()
        await _handle(monkeypatch, _allow(), runner, text="now", session_key="visitor:a")
        assert len(runner.calls[0]["conversation_history"]) == 10


class TestTheFleetCap:
    @pytest.mark.asyncio
    async def test_a_cap_of_one_serialises_two_conversations_and_both_complete(
        self, monkeypatch, sessions, fleet_cap
    ):
        fleet_cap(1)
        runner = _GatedRunner()
        gate = runner.hold("first")
        first = asyncio.create_task(
            _handle(monkeypatch, _allow(), runner, text="first", session_key="visitor:a")
        )
        await _until(lambda: len(runner.calls) == 1)
        second = asyncio.create_task(
            _handle(monkeypatch, _allow(), runner, text="second", session_key="visitor:b")
        )
        await _settle()
        assert len(runner.calls) == 1, "the cap of one let a second run start"
        assert not second.done(), "a capped message was refused instead of queued"

        gate.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2.0)
        assert [r.reply for r in results] == ["re: first", "re: second"]

    def test_the_cap_is_a_declared_hot_reloadable_setting(self, fleet_cap):
        from robothor.settings import get_settings
        from robothor.settings.model import ChannelSettings

        field = ChannelSettings.model_fields["inbound_max_concurrent_runs"]
        extra = field.json_schema_extra or {}
        assert ChannelSettings().inbound_max_concurrent_runs == 8
        assert extra.get("env") == "ROBOTHOR_CHANNEL_MAX_CONCURRENT_RUNS"
        assert extra.get("restart_required") is False
        fleet_cap(3)
        assert get_settings().channels.inbound_max_concurrent_runs == 3


class TestSlackIsBuiltOnIt:
    def test_the_slack_bot_calls_the_shared_pipeline(self):
        """AST, not a substring: this is the "do not fork the pipeline" rule,
        and a docstring mentioning it would satisfy a grep."""
        from robothor.engine.tests.astcheck import called_names, function_def

        branch = function_def("robothor.engine.slack", "_on_message")
        assert "handle_message" in called_names(branch), (
            "Slack no longer runs through channels.inbound, so the gate, the "
            "attribution and the empty-output reply now have two definitions"
        )

    def test_slack_no_longer_calls_the_runner_itself(self):
        from robothor.engine.tests.astcheck import called_names, function_def

        branch = function_def("robothor.engine.slack", "_on_message")
        assert "execute" not in called_names(branch)
