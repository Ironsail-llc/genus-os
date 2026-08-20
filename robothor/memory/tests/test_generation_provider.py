"""Tests for the memory generation provider seam (robothor.memory.generation).

Memory generation (fact/insight extraction, summarization) dispatches through
one seam that defaults to local Ollama but can be pointed at a remote provider
via ROBOTHOR_MEMORY_GENERATION_PROVIDER=openrouter. Remote failures must fall
back to local loudly (WARNING + counter) — never silently.
"""

from __future__ import annotations

import asyncio
import logging
import math
from unittest.mock import AsyncMock

import httpx
import pytest

from robothor.memory import generation


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Isolate provider env vars and module state per test."""
    monkeypatch.delenv(generation.PROVIDER_ENV, raising=False)
    monkeypatch.delenv(generation.REMOTE_MODEL_ENV, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Disable inter-call pacing by default so unrelated tests stay fast;
    # pacing tests set their own interval.
    monkeypatch.setenv(generation.MIN_INTERVAL_ENV, "0")
    generation.remote_fallback_count = 0
    generation._missing_key_logged = False
    generation._last_remote_call_at = 0.0
    generation._pacing_lock = None  # never reuse a lock across event loops


# ─── Default provider: local ollama, no remote calls ────────────────────


async def test_generate_defaults_to_local_ollama(monkeypatch):
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    result = await generation.generate(
        prompt="What?",
        system="Answer.",
        max_tokens=128,
        format={"type": "object"},
        think=False,
    )

    assert result == "local result"
    remote.assert_not_awaited()
    local.assert_awaited_once_with(
        prompt="What?",
        system="Answer.",
        temperature=0.7,
        max_tokens=128,
        model=None,
        think=False,
        format={"type": "object"},
    )


async def test_chat_defaults_to_local_ollama(monkeypatch):
    local = AsyncMock(return_value="local chat")
    remote = AsyncMock(return_value="remote chat")
    monkeypatch.setattr(generation.ollama, "chat", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    messages = [{"role": "user", "content": "hi"}]
    result = await generation.chat(messages)

    assert result == "local chat"
    remote.assert_not_awaited()
    local.assert_awaited_once()


async def test_unknown_provider_value_uses_local(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "banana")
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    assert await generation.generate(prompt="x") == "local result"
    remote.assert_not_awaited()


# ─── provider=openrouter routes to the remote client ─────────────────────


async def test_generate_routes_to_openrouter(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    result = await generation.generate(
        prompt="Extract facts", system="Return JSON.", max_tokens=1024
    )

    assert result == "remote result"
    local.assert_not_awaited()
    remote.assert_awaited_once()
    messages = remote.await_args.kwargs.get("messages") or remote.await_args.args[0]
    assert messages == [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "Extract facts"},
    ]
    assert generation.remote_fallback_count == 0


async def test_chat_routes_to_openrouter(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    local = AsyncMock(return_value="local chat")
    remote = AsyncMock(return_value="remote chat")
    monkeypatch.setattr(generation.ollama, "chat", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    messages = [{"role": "user", "content": "hi"}]
    assert await generation.chat(messages) == "remote chat"
    local.assert_not_awaited()


# ─── Remote failure: loud fallback to local ──────────────────────────────


async def test_remote_failure_falls_back_to_local_with_warning(monkeypatch, caplog):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
        result = await generation.generate(prompt="x")

    assert result == "local result"
    local.assert_awaited_once()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(generation.FALLBACK_MARKER in r.getMessage() for r in warnings)
    assert generation.remote_fallback_count == 1


async def test_fallback_counter_accumulates(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local"))
    monkeypatch.setattr(generation, "_openrouter_chat", AsyncMock(side_effect=RuntimeError("boom")))

    await generation.generate(prompt="a")
    await generation.generate(prompt="b")
    assert generation.remote_fallback_count == 2


# ─── Missing API key: one ERROR, then local ──────────────────────────────


async def test_missing_key_logs_error_once_and_uses_local(monkeypatch, caplog):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.ERROR, logger="robothor.memory.generation"):
        first = await generation.generate(prompt="x")
        second = await generation.generate(prompt="y")

    assert first == "local result"
    assert second == "local result"
    remote.assert_not_awaited()
    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and generation.MISSING_KEY_MARKER in r.getMessage()
    ]
    assert len(errors) == 1  # logged once, not per call


# ─── Think-block stripping ────────────────────────────────────────────────


def test_strip_think_blocks_removes_closed_block():
    assert generation.strip_think_blocks("<think>reasoning</think>answer") == "answer"


def test_strip_think_blocks_handles_unopened_close_tag():
    # Some chat templates strip the opening <think> tag.
    assert generation.strip_think_blocks("reasoning here</think>final") == "final"


def test_strip_think_blocks_multiline_and_multiple():
    text = '<think>\nstep 1\nstep 2\n</think>\n{"a": 1}<think>more</think>'
    assert generation.strip_think_blocks(text) == '{"a": 1}'


def test_strip_think_blocks_passthrough():
    assert generation.strip_think_blocks('{"a": 1}') == '{"a": 1}'


# ─── Direct OpenRouter call: payload, parsing, normalization ─────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    response_payload: dict = {}
    last_init_kwargs: dict = {}
    last_post: dict = {}

    def __init__(self, **kwargs):
        type(self).last_init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_post = {"url": url, "json": json, "headers": headers}
        return _FakeResponse(type(self).response_payload)


async def test_openrouter_chat_payload_and_think_stripping(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _FakeAsyncClient.response_payload = {
        "choices": [{"message": {"content": '<think>hmm</think>{"facts": []}'}}]
    }
    monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeAsyncClient)

    schema = {"type": "object", "properties": {"facts": {"type": "array"}}}
    result = await generation._openrouter_chat(
        [{"role": "user", "content": "extract"}],
        temperature=0.2,
        max_tokens=512,
        format=schema,
        think=False,
    )

    assert result == '{"facts": []}'
    payload = _FakeAsyncClient.last_post["json"]
    # litellm-style "openrouter/" prefix is stripped for the raw API
    assert payload["model"] == "xiaomi/mimo-v2.5"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 512
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == schema
    headers = _FakeAsyncClient.last_post["headers"]
    assert headers["Authorization"] == "Bearer test-key"
    assert _FakeAsyncClient.last_init_kwargs.get("timeout") == generation.REMOTE_TIMEOUT_S


async def test_openrouter_chat_think_true_inflates_max_tokens(monkeypatch):
    """Remote reasoning tokens count against max_tokens (unlike local Ollama,
    where thinking is budgeted separately). think=True must add the same
    overhead ollama.chat adds, or small-budget callers (judge_importance,
    max_tokens=64) get their whole budget eaten by reasoning and an empty
    content back — live-confirmed on xiaomi/mimo-v2.5 (finish_reason=length).
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _FakeAsyncClient.response_payload = {"choices": [{"message": {"content": "0.1"}}]}
    monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeAsyncClient)

    await generation._openrouter_chat(
        [{"role": "user", "content": "rate"}],
        temperature=0.2,
        max_tokens=64,
        format=None,
        think=True,
    )
    payload = _FakeAsyncClient.last_post["json"]
    assert payload["max_tokens"] == 64 + generation.REMOTE_THINKING_OVERHEAD
    assert "reasoning" not in payload


async def test_openrouter_chat_think_false_disables_reasoning(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _FakeAsyncClient.response_payload = {"choices": [{"message": {"content": "[]"}}]}
    monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeAsyncClient)

    await generation._openrouter_chat(
        [{"role": "user", "content": "extract"}],
        temperature=0.2,
        max_tokens=1024,
        format=None,
        think=False,
    )
    payload = _FakeAsyncClient.last_post["json"]
    assert payload["max_tokens"] == 1024  # no inflation without thinking
    assert payload["reasoning"] == {"enabled": False}


async def test_generate_passes_think_to_remote(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    await generation.generate(prompt="x", think=False)
    assert remote.await_args.kwargs["think"] is False

    await generation.generate(prompt="y")
    assert remote.await_args.kwargs["think"] is True


async def test_chat_passes_think_to_remote(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    remote = AsyncMock(return_value="remote chat")
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    await generation.chat([{"role": "user", "content": "hi"}], think=False)
    assert remote.await_args.kwargs["think"] is False


async def test_openrouter_chat_respects_model_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv(generation.REMOTE_MODEL_ENV, "openrouter/deepseek/deepseek-chat")
    _FakeAsyncClient.response_payload = {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeAsyncClient)

    await generation._openrouter_chat(
        [{"role": "user", "content": "x"}], temperature=0.7, max_tokens=64, format=None
    )
    payload = _FakeAsyncClient.last_post["json"]
    assert payload["model"] == "deepseek/deepseek-chat"
    assert "response_format" not in payload


async def test_openrouter_chat_empty_content_raises(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _FakeAsyncClient.response_payload = {
        "choices": [{"message": {"content": "<think>only reasoning</think>"}}]
    }
    monkeypatch.setattr(generation.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(RuntimeError):
        await generation._openrouter_chat(
            [{"role": "user", "content": "x"}], temperature=0.7, max_tokens=64, format=None
        )


# ─── Callers route through the seam ──────────────────────────────────────


async def test_extract_facts_uses_generation_seam(monkeypatch):
    from robothor.memory import facts

    fake = AsyncMock(
        return_value=(
            '[{"fact_text": "Alice prefers tea over coffee", '
            '"category": "preference", "entities": ["Alice"], "confidence": 0.9}]'
        )
    )
    monkeypatch.setattr(generation, "generate", fake)

    result = await facts.extract_facts("Alice said she prefers tea over coffee.")

    fake.assert_awaited()
    assert result and result[0]["fact_text"] == "Alice prefers tea over coffee"


# ─── Rate-limit retry: 429/503 back off before falling back ──────────────
#
# Incident 2026-08-19 04:43: a ~26-call nightly batch hit OpenRouter's rate
# limit; every call abandoned remote after a single 429 and fell back to
# local. 429/503 must be retried with backoff; other 4xx stay fail-fast.


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", generation.OPENROUTER_API_URL)
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _remote_env(monkeypatch):
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _fake_sleep(monkeypatch) -> list[float]:
    """Fake clock: record requested sleeps, return immediately."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(generation.asyncio, "sleep", fake_sleep)
    return sleeps


async def test_429_then_success_retries_without_fallback(monkeypatch, caplog):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=[_status_error(429), "remote result"])
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
        result = await generation.generate(prompt="x")

    assert result == "remote result"
    local.assert_not_awaited()
    assert remote.await_count == 2
    assert len(sleeps) == 1
    assert generation.remote_fallback_count == 0
    # The loud-fallback marker fires only when remote is abandoned.
    assert not any(generation.FALLBACK_MARKER in r.getMessage() for r in caplog.records)


async def test_503_then_success_retries_without_fallback(monkeypatch):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=[_status_error(503), "remote result"])
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    assert await generation.generate(prompt="x") == "remote result"
    local.assert_not_awaited()
    assert remote.await_count == 2
    assert len(sleeps) == 1


async def test_429_exhaustion_falls_back_with_marker(monkeypatch, caplog):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=_status_error(429))
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
        result = await generation.generate(prompt="x")

    assert result == "local result"
    assert remote.await_count == generation.REMOTE_RATE_LIMIT_MAX_ATTEMPTS
    assert len(sleeps) == generation.REMOTE_RATE_LIMIT_MAX_ATTEMPTS - 1
    assert generation.remote_fallback_count == 1
    assert any(generation.FALLBACK_MARKER in r.getMessage() for r in caplog.records)


async def test_400_stays_fail_fast(monkeypatch, caplog):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=_status_error(400))
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
        result = await generation.generate(prompt="x")

    assert result == "local result"
    remote.assert_awaited_once()
    assert sleeps == []
    assert generation.remote_fallback_count == 1
    assert any(generation.FALLBACK_MARKER in r.getMessage() for r in caplog.records)


async def test_timeout_retried_once_then_fallback(monkeypatch, caplog):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
        result = await generation.generate(prompt="x")

    assert result == "local result"
    assert remote.await_count == 2  # one retry, then fallback
    assert len(sleeps) == 1
    assert generation.remote_fallback_count == 1
    assert any(generation.FALLBACK_MARKER in r.getMessage() for r in caplog.records)


async def test_timeout_then_success_does_not_fall_back(monkeypatch):
    _remote_env(monkeypatch)
    _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local result")
    remote = AsyncMock(side_effect=[httpx.ConnectError("reset"), "remote result"])
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    assert await generation.generate(prompt="x") == "remote result"
    local.assert_not_awaited()
    assert generation.remote_fallback_count == 0


async def test_chat_retries_429_too(monkeypatch):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    local = AsyncMock(return_value="local chat")
    remote = AsyncMock(side_effect=[_status_error(429), "remote chat"])
    monkeypatch.setattr(generation.ollama, "chat", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    assert await generation.chat([{"role": "user", "content": "hi"}]) == "remote chat"
    local.assert_not_awaited()
    assert remote.await_count == 2
    assert len(sleeps) == 1


# ─── Retry-After handling ─────────────────────────────────────────────────


def test_backoff_honors_finite_retry_after():
    err = _status_error(429, {"Retry-After": "7"})
    assert generation._remote_backoff_seconds(0, err) == 7.0


def test_backoff_caps_retry_after():
    err = _status_error(429, {"Retry-After": "9999"})
    assert generation._remote_backoff_seconds(0, err) == generation.REMOTE_BACKOFF_CAP_SECONDS


def test_backoff_clamps_negative_retry_after():
    err = _status_error(429, {"Retry-After": "-5"})
    assert generation._remote_backoff_seconds(0, err) == 0.0


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "soon", ""])
def test_backoff_ignores_garbage_retry_after(value):
    # nan slips through min/max clamps and would reach asyncio.sleep(nan).
    err = _status_error(429, {"Retry-After": value})
    wait = generation._remote_backoff_seconds(0, err)
    assert math.isfinite(wait)
    assert 0 < wait <= generation.REMOTE_BACKOFF_CAP_SECONDS


def test_backoff_is_jittered_exponential():
    # attempt 0: base 2s +/-25%; attempt 1: 6s +/-25%; always <= per-sleep cap.
    for _ in range(20):
        first = generation._remote_backoff_seconds(0)
        second = generation._remote_backoff_seconds(1)
        assert 1.5 <= first <= 2.5
        assert 4.5 <= second <= 7.5


async def test_sleep_budget_exhaustion_falls_back_with_marker(monkeypatch, caplog):
    _remote_env(monkeypatch)
    sleeps = _fake_sleep(monkeypatch)
    monkeypatch.setattr(generation, "REMOTE_BACKOFF_BUDGET_SECONDS", 5.0)
    local = AsyncMock(return_value="local result")
    # Retry-After 10s > remaining 5s budget: abandon remote instead of sleeping.
    remote = AsyncMock(side_effect=_status_error(429, {"Retry-After": "10"}))
    monkeypatch.setattr(generation.ollama, "generate", local)
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    with caplog.at_level(logging.WARNING, logger="robothor.memory.generation"):
        result = await generation.generate(prompt="x")

    assert result == "local result"
    remote.assert_awaited_once()
    assert sleeps == []
    assert generation.remote_fallback_count == 1
    assert any(generation.FALLBACK_MARKER in r.getMessage() for r in caplog.records)


# ─── Inter-call pacing ────────────────────────────────────────────────────


async def test_pacing_spaces_concurrent_remote_calls(monkeypatch):
    _remote_env(monkeypatch)
    monkeypatch.setenv(generation.MIN_INTERVAL_ENV, "1.5")
    sleeps = _fake_sleep(monkeypatch)
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    await asyncio.gather(generation.generate(prompt="a"), generation.generate(prompt="b"))

    assert remote.await_count == 2
    # First call goes straight through; the second waits out the interval.
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 1.5


async def test_pacing_zero_disables(monkeypatch):
    _remote_env(monkeypatch)
    monkeypatch.setenv(generation.MIN_INTERVAL_ENV, "0")
    sleeps = _fake_sleep(monkeypatch)
    remote = AsyncMock(return_value="remote result")
    monkeypatch.setattr(generation, "_openrouter_chat", remote)

    await generation.generate(prompt="a")
    await generation.generate(prompt="b")

    assert sleeps == []


def test_min_interval_env_override_and_defaults(monkeypatch):
    monkeypatch.delenv(generation.MIN_INTERVAL_ENV, raising=False)
    assert generation._min_interval_s() == generation.DEFAULT_MIN_INTERVAL_S

    monkeypatch.setenv(generation.MIN_INTERVAL_ENV, "2.5")
    assert generation._min_interval_s() == 2.5

    for garbage in ("soon", "nan", "inf", "-1"):
        monkeypatch.setenv(generation.MIN_INTERVAL_ENV, garbage)
        assert generation._min_interval_s() == generation.DEFAULT_MIN_INTERVAL_S
