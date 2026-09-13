"""``SlackBot.slack_send`` — the outbound half, driven directly.

It was defined *inside* ``SlackBot.start()``, so the only test that called
``start()`` returned before reaching it and nothing exercised it at all. That is
how it spent its whole life returning ``None``: a sender nothing consumed, whose
return value nothing checked.

Now that ``delivery.channel: slack`` resolves to a real channel, two properties
decide whether an operator is told the truth about a Slack briefing:

* **What it returns is the only evidence it landed.** ``chat_postMessage``
  returns a ``SlackResponse``; the platform's message id is ``ts``, not
  ``message_id``, and ``channel_bus.on_post_delivery`` writes one
  ``channel_message_map`` row per id — so an empty id list means a Slack
  briefing can never be replied to.
* **A failure partway through must not lose the chunks that landed.**
  ``chat_postMessage`` raises ``SlackApiError``; letting it propagate turned a
  ``partial:2/3`` into a total failure and dropped the ids of the two messages
  the operator can actually see.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.channels.base import acknowledged_messages
from robothor.engine.slack import MAX_SLACK_LENGTH, SlackBot


class _FakeResponse:
    """Stand-in for ``slack_sdk`` ``SlackResponse``: mapping access over ``data``."""

    def __init__(self, ts: str) -> None:
        self.data = {"ok": True, "ts": ts, "channel": "C1"}

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class _FakeClient:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.posted: list[str] = []
        self._fail_on = fail_on or set()
        self._n = 0

    async def chat_postMessage(  # noqa: N802 — the Slack SDK's own method name
        self, *, channel: str, text: str, **_: Any
    ) -> _FakeResponse:
        self._n += 1
        if self._n in self._fail_on:
            raise RuntimeError(f"SlackApiError: chunk {self._n} rejected")
        self.posted.append(text)
        return _FakeResponse(ts=f"1700000000.{self._n:06d}")


def _bot(client: Any) -> SlackBot:
    bot = SlackBot(SimpleNamespace(), SimpleNamespace(tenant_id="default"))
    bot._app = SimpleNamespace(client=client)
    return bot


class TestWhatItReturns:
    @pytest.mark.asyncio
    async def test_one_entry_per_posted_chunk(self):
        client = _FakeClient()
        sent = await _bot(client).slack_send("C1", "x" * (MAX_SLACK_LENGTH * 2 + 100))

        assert len(client.posted) == 3
        assert len(sent) == 3

    @pytest.mark.asyncio
    async def test_the_platform_id_is_the_slack_ts(self):
        """``channel_bus`` writes one ``channel_message_map`` row per id; with
        none, a Slack briefing can never be replied to."""
        client = _FakeClient()
        sent = await _bot(client).slack_send("C1", "a short briefing")

        count, ids = acknowledged_messages(sent)
        assert count == 1
        assert ids == ["1700000000.000001"], "the Slack ts did not reach platform_ids"

    @pytest.mark.asyncio
    async def test_no_app_acknowledges_nothing(self):
        bot = SlackBot(SimpleNamespace(), SimpleNamespace(tenant_id="default"))
        assert await bot.slack_send("C1", "hello") == []


class TestAFailurePartwayThrough:
    @pytest.mark.asyncio
    async def test_the_chunks_that_landed_survive_in_the_return_value(self):
        client = _FakeClient(fail_on={2})
        sent = await _bot(client).slack_send("C1", "x" * (MAX_SLACK_LENGTH * 2 + 100))

        count, ids = acknowledged_messages(sent)
        assert count == 2, "a mid-chunk failure lost the chunks that did land"
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_every_chunk_failing_is_an_empty_list_not_an_exception(self):
        client = _FakeClient(fail_on={1, 2, 3})
        sent = await _bot(client).slack_send("C1", "x" * (MAX_SLACK_LENGTH * 2 + 100))
        assert sent == []


class TestItIsRegisteredWithItsChunkSize:
    def test_start_registers_the_bound_sender_with_the_slack_limit(self):
        """Without the chunk size the shim cannot tell a truncated briefing from
        a delivered one — it would count 2 acknowledgements against 1 expected.

        AST, not a substring: the old form searched ``start``'s source text, and
        the three names it looked for all appear in the docstring of
        ``slack_send`` one method below.
        """
        from robothor.engine.tests.astcheck import called_names, function_def, keywords_of_call

        start = function_def("robothor.engine.slack", "start")
        assert "register_platform_sender" in called_names(start), (
            "SlackBot.start no longer registers its sender, so nothing can reach slack_send"
        )
        assert "chunk_size" in keywords_of_call(start, "register_platform_sender"), (
            "the sender is registered without a chunk size, so a truncated Slack "
            "briefing reads as delivered"
        )
