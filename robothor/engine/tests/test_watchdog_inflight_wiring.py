"""The wait window must actually open around the provider call.

Companion to test_watchdog_llm_inflight.py, which proves the watchdog SEMANTICS.
This file proves the WIRING, and it is the half that decides whether any of it
runs in production. A window opened against a mocked property, or across a task
boundary where the ContextVar does not follow, would satisfy a careless test and
still leave every cron run dying at `last activity: session_started`.

So every test here sets the real `_active_watchdog_var` and observes
`waiting_on` FROM INSIDE the patched provider call.

The non-streaming path is the one that matters: `_do_llm_call` only dispatches
to `_call_llm_streaming` when on_content/on_stream_event is set, and cron and
workflow runs pass neither. That is why fault-injecting through the streaming
path looked healthy on 2026-08-27 while cron runs were failing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.llm_client import (
    LLM_REQUEST_TIMEOUT_OLLAMA,
    LLMClient,
)
from robothor.engine.stall_watchdog import _StallWatchdog, _active_watchdog_var

LOCAL = "ollama_chat/qwen3.8:27b"


def _response(content: str = "ok") -> Any:
    """Minimal litellm-shaped response."""
    from litellm import ModelResponse

    return ModelResponse(
        choices=[{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    )


def _watchdog() -> _StallWatchdog:
    # hard_timeout > 0 so the loop would run in production; these tests never
    # start() it — they inspect state, not cancellation.
    return _StallWatchdog(stall_timeout=0, hard_timeout=3600, tick_seconds=0.05)


@pytest.mark.asyncio
async def test_a_non_streaming_call_opens_a_window_named_for_the_model():
    """Observed from inside the await — the only place it proves anything."""
    wd = _watchdog()
    seen: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen["waiting_on"] = wd.waiting_on
        return _response()

    token = _active_watchdog_var.set(wd)
    try:
        with patch("litellm.acompletion", side_effect=fake_acompletion):
            await LLMClient()._call_llm(
                messages=[{"role": "user", "content": "hi"}], models=[LOCAL], tools=[]
            )
    finally:
        _active_watchdog_var.reset(token)

    assert seen["waiting_on"] == f"llm_inflight:{LOCAL}"
    assert wd.waiting_on == ""  # closed on the way out
    assert wd.attributed_wait_seconds > 0


@pytest.mark.asyncio
async def test_the_window_closes_when_the_provider_raises():
    """A leaked window would pause the stall clock for the rest of the run."""
    wd = _watchdog()

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("provider exploded")

    token = _active_watchdog_var.set(wd)
    try:
        with patch("litellm.acompletion", side_effect=boom):
            await LLMClient()._call_llm(
                messages=[{"role": "user", "content": "hi"}], models=[LOCAL], tools=[]
            )
    finally:
        _active_watchdog_var.reset(token)

    assert wd.waiting_on == ""


@pytest.mark.asyncio
async def test_the_budget_is_the_models_own_per_call_timeout():
    """The window may only buy the ceiling that already governs the call."""
    wd = _watchdog()
    seen: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen["budget"] = wd._wait.budget if wd._wait else None
        return _response()

    token = _active_watchdog_var.set(wd)
    try:
        with patch("litellm.acompletion", side_effect=fake_acompletion):
            await LLMClient()._call_llm(
                messages=[{"role": "user", "content": "hi"}], models=[LOCAL], tools=[]
            )
    finally:
        _active_watchdog_var.reset(token)

    assert seen["budget"] == LLM_REQUEST_TIMEOUT_OLLAMA


@pytest.mark.asyncio
async def test_no_watchdog_bound_is_harmless():
    """Sub-agents and out-of-loop callers run with no watchdog in context."""
    token = _active_watchdog_var.set(None)
    try:
        with patch("litellm.acompletion", side_effect=lambda **k: _async(_response())):
            result = await LLMClient()._call_llm(
                messages=[{"role": "user", "content": "hi"}], models=[LOCAL], tools=[]
            )
    finally:
        _active_watchdog_var.reset(token)
    assert result is not None


async def _async(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_a_cron_run_no_longer_dies_with_last_activity_session_started():
    """The 2026-08-27 incident, reproduced end to end.

    A cron run whose only LLM call is slower than its cloud-era stall budget
    must now survive. Before the wait window this cancelled with
    `last activity: session_started`.
    """
    wd = _StallWatchdog(stall_timeout=1, hard_timeout=3600, tick_seconds=0.05)

    async def slow(**kwargs: Any) -> Any:
        await asyncio.sleep(2.0)  # 2x the stall budget
        return _response("finally")

    async def run_it() -> Any:
        wd.touch("session_started")
        with patch("litellm.acompletion", side_effect=slow):
            return await LLMClient()._call_llm(
                messages=[{"role": "user", "content": "hi"}], models=[LOCAL], tools=[]
            )

    token = _active_watchdog_var.set(wd)
    try:
        task = asyncio.create_task(run_it())
        wd.start(task)
        result = await task
    finally:
        _active_watchdog_var.reset(token)

    assert result is not None
    assert wd.was_stall_timeout is False
    assert "session_started" not in wd.abort_reason
