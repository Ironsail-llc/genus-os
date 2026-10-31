"""Plugin channels can stream through the shared inbound pipeline.

A plugin channel cannot reach ``runner.execute`` itself; it hands a message to
``channels.inbound.handle_message`` and gets the reply back. With an optional
``stream`` callback and ``ROBOTHOR_CHANNEL_STREAMING`` on, the run's cumulative
text reaches that callback as it is written (the runner's ``on_content``), and
the run executes inside a channel-streaming scope, so a transient stream failure
retries in place. With the flag off, or no callback, the call is today's, key
for key.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine.channel_streaming import in_channel_stream_scope
from robothor.engine.tests.test_channel_inbound_pipeline import (  # noqa: F401 - fixtures
    _allow,
    _handle,
    _no_session,
    _Run,
    _Runner,
    store,
)


@pytest.fixture
def flag(monkeypatch):
    from robothor.settings import reset_settings

    def _set(on: bool) -> None:
        if on:
            monkeypatch.setenv("ROBOTHOR_CHANNEL_STREAMING", "true")
        else:
            monkeypatch.delenv("ROBOTHOR_CHANNEL_STREAMING", raising=False)
        reset_settings()

    yield _set
    monkeypatch.delenv("ROBOTHOR_CHANNEL_STREAMING", raising=False)
    reset_settings()


class _ScopeRecordingRunner(_Runner):
    def __init__(self) -> None:
        super().__init__(_Run())
        self.scoped: list[bool] = []

    async def execute(self, **kw: Any) -> Any:
        self.scoped.append(in_channel_stream_scope())
        return await super().execute(**kw)


async def _stream(_text: str) -> None:
    return None


@pytest.mark.asyncio
async def test_flag_on_passes_the_stream_as_on_content(monkeypatch, flag):
    """AC9."""
    flag(True)
    runner = _ScopeRecordingRunner()

    result = await _handle(monkeypatch, _allow(), runner, stream=_stream)

    assert runner.calls[0]["on_content"] is _stream
    assert runner.scoped == [True]
    assert result.reply == "the answer"


@pytest.mark.asyncio
async def test_flag_off_ignores_the_stream(monkeypatch, flag):
    """AC8: byte-identical kwargs with the flag off, even when a stream is passed."""
    flag(False)
    plain, streamed = _ScopeRecordingRunner(), _ScopeRecordingRunner()

    # record_turn=False: the harness's fake session is shared, and the first
    # call's recorded turn would otherwise become the second call's history.
    await _handle(monkeypatch, _allow(), plain, record_turn=False)
    await _handle(monkeypatch, _allow(), streamed, stream=_stream, record_turn=False)

    assert "on_content" not in streamed.calls[0]
    assert streamed.calls[0] == plain.calls[0]
    assert streamed.scoped == [False]


@pytest.mark.asyncio
async def test_no_stream_keeps_the_call_unchanged_with_the_flag_on(monkeypatch, flag):
    flag(True)
    runner = _ScopeRecordingRunner()

    await _handle(monkeypatch, _allow(), runner)

    assert "on_content" not in runner.calls[0]
    assert runner.scoped == [False]
