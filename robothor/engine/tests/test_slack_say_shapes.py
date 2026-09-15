"""Slack's four ``say()`` shapes, pinned against the version before the refactor.

Extracting the inbound pipeline was allowed to change nothing a Slack workspace
can observe. Three call shapes drifted anyway: the pairing refusal gained
``mrkdwn=True``, and the two sentences moved from a positional argument to
``text=…``. All three are ASCII with no Slack markup, so nothing renders
differently *today* — which is exactly why it needs a test rather than an
argument. The next refusal sentence that carries an underscore or an asterisk is
the one that renders as italics in a stranger's DM.

The shapes, as ``engine/slack.py`` sent them before this branch:

===================  ==========================================
what                 the call
===================  ==========================================
a pairing refusal    ``say(text=<the refusal>)``
each output chunk    ``say(text=<chunk>, mrkdwn=True)``
a run with no text   ``say("I processed your request …")``
a run that failed    ``say("Something went wrong. Please try again.")``
===================  ==========================================

And the fourth line is also the reason ``say`` belongs inside the ``try``: a
``say`` that raises used to be answered with the failure sentence, and after the
refactor it propagated into Bolt.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.channels import inbound
from robothor.engine.channels.access import AccessDecision
from robothor.engine.slack import MAX_SLACK_LENGTH, SlackBot


class _Say:
    def __init__(self, raises: Exception | None = None, raise_on: int = 1) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._raises = raises
        self._raise_on = raise_on

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self._raises is not None and len(self.calls) == self._raise_on:
            raise self._raises


class _Config:
    tenant_id = "00000000-0000-0000-0000-000000000000"


def _bot(output: str | None = "the answer", raises: Exception | None = None) -> SlackBot:
    class _Run:
        output_text = output

    class _Runner:
        async def execute(self, **_kw: Any) -> Any:
            if raises is not None:
                raise raises
            return _Run()

    return SlackBot(_Runner(), _Config())


def _event(text: str = "hello") -> dict[str, Any]:
    return {"text": text, "channel": "D0000000000", "user": "U0000000000", "channel_type": "im"}


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    class _Session:
        history: list[Any] = []

    monkeypatch.setattr(inbound, "get_shared_session", lambda _key: _Session())


def _gate(monkeypatch, decision: AccessDecision) -> None:
    async def _evaluate(*_a: Any, **_kw: Any) -> AccessDecision:
        return decision

    monkeypatch.setattr(inbound.access, "evaluate", _evaluate)
    monkeypatch.setattr(SlackBot, "_access_mode", lambda self: "open")


class TestTheFourShapes:
    @pytest.mark.asyncio
    async def test_a_refusal_is_a_keyword_text_and_no_mrkdwn(self, monkeypatch):
        _gate(monkeypatch, AccessDecision(allowed=False, refusal="here is a code: ABC234"))
        say = _Say()
        await _bot()._on_message(_event(), say)
        assert say.calls == [((), {"text": "here is a code: ABC234"})]

    @pytest.mark.asyncio
    async def test_an_empty_refusal_says_nothing_at_all(self, monkeypatch):
        _gate(monkeypatch, AccessDecision(allowed=False, refusal=""))
        say = _Say()
        await _bot()._on_message(_event(), say)
        assert say.calls == []

    @pytest.mark.asyncio
    async def test_each_output_chunk_is_text_plus_mrkdwn(self, monkeypatch):
        _gate(monkeypatch, AccessDecision(allowed=True))
        say = _Say()
        await _bot(output="the answer")._on_message(_event(), say)
        assert say.calls == [((), {"text": "the answer", "mrkdwn": True})]

    @pytest.mark.asyncio
    async def test_a_long_answer_is_chunked_exactly_as_before(self, monkeypatch):
        _gate(monkeypatch, AccessDecision(allowed=True))
        say = _Say()
        body = "x" * (MAX_SLACK_LENGTH * 2 + 10)
        await _bot(output=body)._on_message(_event(), say)

        from robothor.engine.slack import _split_text

        assert [kwargs["text"] for _args, kwargs in say.calls] == _split_text(
            body, MAX_SLACK_LENGTH
        )
        assert all(kwargs["mrkdwn"] is True for _args, kwargs in say.calls)

    @pytest.mark.asyncio
    async def test_no_output_is_a_positional_sentence(self, monkeypatch):
        _gate(monkeypatch, AccessDecision(allowed=True))
        say = _Say()
        await _bot(output="")._on_message(_event(), say)
        assert say.calls == [(("I processed your request but have no output to share.",), {})]

    @pytest.mark.asyncio
    async def test_a_failed_run_is_a_positional_sentence(self, monkeypatch):
        _gate(monkeypatch, AccessDecision(allowed=True))
        say = _Say()
        await _bot(raises=RuntimeError("the model is down"))._on_message(_event(), say)
        assert say.calls == [(("Something went wrong. Please try again.",), {})]


class TestSayFailuresAreStillCaught:
    @pytest.mark.asyncio
    async def test_a_say_that_raises_is_answered_with_the_failure_sentence(self, monkeypatch):
        """Before the refactor this was inside the try. After it, the exception
        went to Bolt and the person got nothing."""
        _gate(monkeypatch, AccessDecision(allowed=True))
        say = _Say(raises=RuntimeError("slack rejected it"), raise_on=1)
        await _bot(output="the answer")._on_message(_event(), say)
        assert say.calls[-1] == (("Something went wrong. Please try again.",), {})

    @pytest.mark.asyncio
    async def test_a_second_failure_does_not_propagate(self, monkeypatch):
        """If the apology cannot be delivered either, the handler still returns:
        an exception out of `_on_message` reaches Bolt, which logs it without
        the context this one has."""
        _gate(monkeypatch, AccessDecision(allowed=True))

        class _AlwaysFails(_Say):
            async def __call__(self, *args: Any, **kwargs: Any) -> None:
                self.calls.append((args, kwargs))
                raise RuntimeError("slack is down")

        await _bot(output="the answer")._on_message(_event(), _AlwaysFails())
