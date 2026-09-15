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
