"""``ask_user`` on a run a PLUGIN channel started.

The platform's only caller of ``Channel.ask`` knew four trigger types by name,
and a run started by a plugin channel matched none of them — so the first
channel to ship as a plugin implemented ``ask``, tested it, documented it, and
could never be reached by the tool that is the whole reason ``ask`` exists. That
is this repository's canonical defect: a control built, wired, tested, and aimed
at nothing.

The fix is deliberately not a Teams branch. A run that arrives through
``channels.inbound`` records ``channel:<name>:<reply target>`` as its trigger
detail, and this tool reads the channel and the target back out of it — so any
plugin channel that receives gets ``ask`` for free, and none of them is named
here.

Three things have to be true, and only the first was:

1. the tool must not refuse the run;
2. it must resolve the channel the run actually came from;
3. it must hand that channel a real ``target`` and a real ``addressee``. The
   addressee is what binds the pending question to the person who was asked;
   without it, in a Teams channel, anyone who can see the card can answer it.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.tools.handlers import ask_user as ask_user_module
from robothor.identity import IdentityContext

TENANT = "00000000-0000-0000-0000-000000000000"
CONVERSATION = "19:a-room.thread.v2"
ALICE = "aaaaaaaa-0000-0000-0000-00000000aaaa"


class _Channel:
    """A plugin channel that records how it was asked."""

    name = "acme"
    inbound_router = object()

    def __init__(self, answer: str | None = "yes") -> None:
        self.asks: list[dict[str, Any]] = []
        self._answer = answer

    async def send(self, target: str, text: str, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def ask(
        self,
        question: str,
        options: Any = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        self.asks.append(
            {
                "question": question,
                "options": list(options),
                "timeout": timeout,
                "target": target,
                "addressee": addressee,
            }
        )
        return self._answer


class _Ctx:
    def __init__(self, identity: IdentityContext | None) -> None:
        self.run_id = "run-1"
        self.tenant_id = TENANT
        self.agent_id = "main"
        self.identity = identity


def _identity(channel: str = "acme") -> IdentityContext:
    return IdentityContext(
        tenant_id=TENANT,
        channel=channel,
        identifier=ALICE,
        verified=True,
        tenant_user_id="alice",
        role="member",
    )


@pytest.fixture
def armed(monkeypatch):
    """One armed plugin channel, and a run that came in over it."""
    channel = _Channel()

    def _install(trigger_detail: str = f"channel:acme:{CONVERSATION}") -> _Channel:
        monkeypatch.setattr(
            ask_user_module.tracking,
            "get_run",
            lambda _run_id: {"trigger_type": "channel", "trigger_detail": trigger_detail},
        )
        from robothor.engine import channels as channels_module

        monkeypatch.setattr(
            channels_module, "get_channel", lambda name: channel if name == "acme" else None
        )
        return channel

    return _install


@pytest.fixture(autouse=True)
def _no_durable_row(monkeypatch):
    """The question row and the status stream are not what this file is about."""

    class _Asked:
        id = "q-1"
        expires_at = None

    monkeypatch.setattr(
        ask_user_module.agent_questions, "ask_question", lambda **_kw: _Asked(), raising=True
    )
    monkeypatch.setattr(
        ask_user_module.agent_questions, "answer_question", lambda *_a, **_kw: None, raising=True
    )

    async def _emit(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(ask_user_module.run_status, "emit_status", _emit)


class TestAPluginChannelRunCanAsk:
    @pytest.mark.asyncio
    async def test_the_run_is_not_refused_for_having_no_person(self, armed):
        """The refusal is for cron, hooks and sub-agents — runs nobody is
        watching. Somebody typed this one."""
        armed()
        result = await ask_user_module._handle_ask_user(
            {"question": "Ship it?", "options": ["yes", "no"]}, _Ctx(_identity())
        )
        assert "error" not in result, result

    @pytest.mark.asyncio
    async def test_the_channel_the_run_came_from_is_the_channel_that_is_asked(self, armed):
        channel = armed()
        await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(_identity()))
        assert len(channel.asks) == 1, "the plugin channel was never asked"

    @pytest.mark.asyncio
    async def test_the_question_is_aimed_at_the_conversation_it_came_from(self, armed):
        channel = armed()
        await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(_identity()))
        assert channel.asks[0]["target"] == CONVERSATION, (
            "with no target a channel cannot know where to put the question"
        )

    @pytest.mark.asyncio
    async def test_the_sender_is_the_addressee(self, armed):
        """The binding that makes a card in a shared room safe to send. It was
        always empty: the check compared the identity's CHANNEL ('acme') with
        the TRIGGER ('channel'), which can never match."""
        channel = armed()
        await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(_identity()))
        assert channel.asks[0]["addressee"] == ALICE

    @pytest.mark.asyncio
    async def test_the_answer_comes_back_to_the_caller(self, armed):
        armed()
        result = await ask_user_module._handle_ask_user(
            {"question": "Ship it?", "options": ["yes", "no"]}, _Ctx(_identity())
        )
        assert result["answered"] is True
        assert result["answer"] == "yes"


class TestWhatItStillRefuses:
    @pytest.mark.asyncio
    async def test_an_identity_bound_to_another_channel_is_not_the_addressee(self, armed):
        """An unaddressed ask falls back to the platform's own authorization;
        binding it to an identity proved on a DIFFERENT surface would aim it at
        somebody who is not in this conversation."""
        channel = armed()
        await ask_user_module._handle_ask_user(
            {"question": "Ship it?"}, _Ctx(_identity(channel="telegram"))
        )
        assert channel.asks[0]["addressee"] == ""

    @pytest.mark.asyncio
    async def test_an_unverified_identity_is_not_the_addressee(self, armed):
        channel = armed()
        unverified = IdentityContext(
            tenant_id=TENANT, channel="acme", identifier=ALICE, verified=False, role="viewer"
        )
        await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(unverified))
        assert channel.asks[0]["addressee"] == ""

    @pytest.mark.asyncio
    async def test_a_run_whose_detail_names_no_channel_asks_nobody(self, armed):
        """A `channel` run that did not come through the shared pipeline has no
        channel and no target. Refused rather than aimed at a guess."""
        channel = armed(trigger_detail="")
        result = await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(None))
        assert channel.asks == []
        assert result.get("answered") is not True

    @pytest.mark.asyncio
    async def test_a_channel_that_is_not_armed_asks_nobody(self, monkeypatch):
        monkeypatch.setattr(
            ask_user_module.tracking,
            "get_run",
            lambda _run_id: {
                "trigger_type": "channel",
                "trigger_detail": f"channel:not-installed:{CONVERSATION}",
            },
        )
        result = await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(None))
        assert result.get("answered") is not True

    @pytest.mark.asyncio
    async def test_a_cron_run_is_still_refused(self, monkeypatch):
        monkeypatch.setattr(
            ask_user_module.tracking,
            "get_run",
            lambda _run_id: {"trigger_type": "cron", "trigger_detail": ""},
        )
        result = await ask_user_module._handle_ask_user({"question": "Ship it?"}, _Ctx(None))
        assert "error" in result


class TestTheDetailIsWrittenByThePipeline:
    @pytest.mark.asyncio
    async def test_the_shared_inbound_pipeline_records_the_channel_and_the_target(self):
        """The one thing that makes the above generic: `channels.inbound` writes
        the detail, so every channel that receives gets `ask` without the tool
        learning its name."""
        from robothor.engine.channels import inbound
        from robothor.engine.channels.access import AccessDecision
        from robothor.engine.models import TriggerType

        calls: list[dict[str, Any]] = []

        class _Runner:
            async def execute(self, **kw: Any) -> Any:
                calls.append(kw)

                class _Run:
                    output_text = "ok"

                return _Run()

        async def _allow(*_a: Any, **_kw: Any) -> AccessDecision:
            return AccessDecision(allowed=True)

        import robothor.engine.channels.access as access_module

        original = access_module.evaluate
        access_module.evaluate = _allow  # type: ignore[assignment]
        try:
            from robothor.engine import chat as chat_module

            original_session = chat_module.get_shared_session

            class _Session:
                history: list[Any] = []

            inbound.get_shared_session = lambda _key: _Session()  # type: ignore[assignment]
            try:
                await inbound.handle_message(
                    channel="acme",
                    native_id=ALICE,
                    text="hello",
                    runner=_Runner(),
                    tenant_id=TENANT,
                    session_key=f"agent:main:acme:{CONVERSATION}",
                    trigger_type=TriggerType.CHANNEL,
                    reply_target=CONVERSATION,
                )
            finally:
                inbound.get_shared_session = original_session  # type: ignore[assignment]
        finally:
            access_module.evaluate = original  # type: ignore[assignment]

        assert calls[0]["trigger_detail"] == f"channel:acme:{CONVERSATION}"
