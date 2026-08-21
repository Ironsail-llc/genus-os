"""Tests for memory-generation alerting (robothor.memory.generation).

The generation seam degrades quietly by design: a remote failure falls back
to local Ollama, and a local failure is swallowed by the callers' best-effort
retry loops (facts.extract_facts logs "failed after N attempts" and returns
[]). That made a multi-day outage invisible — hundreds of HTTP-429/503 events
with nothing paging.

These tests pin the escalation: a sustained remote→local fallback streak
raises ONE latched warning alert, and losing both legs (no generation path
left) raises a critical alert. The happy path stays silent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from robothor.memory import generation


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Isolate provider env vars, counters, and alert latches per test."""
    monkeypatch.delenv(generation.PROVIDER_ENV, raising=False)
    monkeypatch.delenv(generation.REMOTE_MODEL_ENV, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(generation.MIN_INTERVAL_ENV, "0")
    generation.remote_fallback_count = 0
    generation._consecutive_fallbacks = 0
    generation._missing_key_logged = False
    generation._last_remote_call_at = 0.0
    generation._pacing_lock = None
    generation._alert_latched_at = {}


@pytest.fixture
def alert_calls(monkeypatch) -> list[dict]:
    """Capture robothor.engine.alerts.alert() calls.

    Patched at the source module because generation.py imports it lazily
    inside the alerting helper (memory must not import engine at module
    scope), so there is no module-level name to patch on generation.
    """
    calls: list[dict] = []

    async def fake_alert(level, title, body, *, channel="telegram", metadata=None):
        calls.append(
            {
                "level": level,
                "title": title,
                "body": body,
                "channel": channel,
                "metadata": metadata,
            }
        )
        return True

    monkeypatch.setattr("robothor.engine.alerts.alert", fake_alert)
    return calls


def _remote_env(monkeypatch) -> None:
    monkeypatch.setenv(generation.PROVIDER_ENV, "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", generation.OPENROUTER_API_URL)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# ─── (a) fallback streak → exactly one latched warning ───────────────────


async def test_fallback_streak_fires_one_latched_warning_alert(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local result"))
    monkeypatch.setattr(
        generation,
        "_openrouter_chat_with_retry",
        AsyncMock(side_effect=_http_error(429)),
    )

    # Drive well past the threshold — a 429 storm must not become an alert storm.
    for _ in range(generation.FALLBACK_STREAK_THRESHOLD * 3):
        assert await generation.generate(prompt="extract facts") == "local result"

    assert len(alert_calls) == 1, f"expected one latched alert, got {len(alert_calls)}"
    fired = alert_calls[0]
    assert fired["level"] == "warning"
    body = fired["body"]
    assert generation._remote_model() in body
    assert "HTTPStatusError" in body
    assert "ollama" in body.lower()


async def test_fallback_streak_below_threshold_is_silent(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local result"))
    monkeypatch.setattr(
        generation,
        "_openrouter_chat_with_retry",
        AsyncMock(side_effect=_http_error(503)),
    )

    for _ in range(generation.FALLBACK_STREAK_THRESHOLD - 1):
        await generation.generate(prompt="extract facts")

    assert alert_calls == []


async def test_fallback_streak_latch_rearms_after_remote_success(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local result"))
    remote = AsyncMock(side_effect=_http_error(429))
    monkeypatch.setattr(generation, "_openrouter_chat_with_retry", remote)

    for _ in range(generation.FALLBACK_STREAK_THRESHOLD):
        await generation.generate(prompt="extract facts")
    assert len(alert_calls) == 1

    # Remote recovers: streak resets and the latch re-arms.
    remote.side_effect = None
    remote.return_value = "remote result"
    assert await generation.generate(prompt="extract facts") == "remote result"

    remote.side_effect = _http_error(429)
    for _ in range(generation.FALLBACK_STREAK_THRESHOLD):
        await generation.generate(prompt="extract facts")

    assert len(alert_calls) == 2
    assert [c["level"] for c in alert_calls] == ["warning", "warning"]


# ─── (b) both legs down → critical ───────────────────────────────────────


async def test_both_legs_failing_fires_critical_alert(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(
        generation,
        "_openrouter_chat_with_retry",
        AsyncMock(side_effect=_http_error(429)),
    )
    monkeypatch.setattr(
        generation.ollama,
        "generate",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    )

    with pytest.raises(httpx.ConnectError):
        await generation.generate(prompt="extract facts")

    assert len(alert_calls) == 1
    fired = alert_calls[0]
    assert fired["level"] == "critical"
    body = fired["body"]
    assert generation._remote_model() in body
    assert generation.ollama.GENERATION_MODEL in body
    assert "HTTPStatusError" in body
    assert "ConnectError" in body


async def test_both_legs_failing_via_chat_fires_critical_alert(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(
        generation,
        "_openrouter_chat_with_retry",
        AsyncMock(side_effect=RuntimeError("empty content from remote model")),
    )
    monkeypatch.setattr(
        generation.ollama,
        "chat",
        AsyncMock(side_effect=TimeoutError("ollama timed out")),
    )

    with pytest.raises(TimeoutError):
        await generation.chat([{"role": "user", "content": "hi"}])

    assert [c["level"] for c in alert_calls] == ["critical"]
    assert "TimeoutError" in alert_calls[0]["body"]


async def test_critical_alert_is_latched(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(
        generation,
        "_openrouter_chat_with_retry",
        AsyncMock(side_effect=_http_error(503)),
    )
    monkeypatch.setattr(
        generation.ollama,
        "generate",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    )

    for _ in range(generation.FALLBACK_STREAK_THRESHOLD * 3):
        with pytest.raises(httpx.ConnectError):
            await generation.generate(prompt="extract facts")

    criticals = [c for c in alert_calls if c["level"] == "critical"]
    assert len(criticals) == 1, f"expected one latched critical, got {len(criticals)}"


async def test_local_only_provider_failure_fires_critical_alert(monkeypatch, alert_calls):
    # Default provider (ollama): there is no remote leg, so a local failure
    # is already "no generation path left".
    monkeypatch.setattr(
        generation.ollama,
        "generate",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    )

    with pytest.raises(httpx.ConnectError):
        await generation.generate(prompt="extract facts")

    assert [c["level"] for c in alert_calls] == ["critical"]
    assert "ConnectError" in alert_calls[0]["body"]


async def test_critical_latch_rearms_after_a_successful_generation(monkeypatch, alert_calls):
    local = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(generation.ollama, "generate", local)

    with pytest.raises(httpx.ConnectError):
        await generation.generate(prompt="extract facts")
    assert len(alert_calls) == 1

    local.side_effect = None
    local.return_value = "local result"
    assert await generation.generate(prompt="extract facts") == "local result"

    local.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(httpx.ConnectError):
        await generation.generate(prompt="extract facts")

    assert len(alert_calls) == 2


# ─── (c) happy paths stay silent ─────────────────────────────────────────


async def test_local_happy_path_raises_no_alert(monkeypatch, alert_calls):
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local result"))
    monkeypatch.setattr(generation.ollama, "chat", AsyncMock(return_value="local chat"))

    assert await generation.generate(prompt="extract facts") == "local result"
    assert await generation.chat([{"role": "user", "content": "hi"}]) == "local chat"
    assert alert_calls == []


async def test_remote_happy_path_raises_no_alert(monkeypatch, alert_calls):
    _remote_env(monkeypatch)
    monkeypatch.setattr(
        generation, "_openrouter_chat_with_retry", AsyncMock(return_value="remote result")
    )
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local result"))

    for _ in range(generation.FALLBACK_STREAK_THRESHOLD * 2):
        assert await generation.generate(prompt="extract facts") == "remote result"

    assert alert_calls == []


# ─── alerting must never break a memory write ────────────────────────────


async def test_alert_dispatch_failure_does_not_break_generation(monkeypatch):
    async def exploding_alert(*_args, **_kwargs):
        raise RuntimeError("alert channel down")

    monkeypatch.setattr("robothor.engine.alerts.alert", exploding_alert)
    _remote_env(monkeypatch)
    monkeypatch.setattr(generation.ollama, "generate", AsyncMock(return_value="local result"))
    monkeypatch.setattr(
        generation,
        "_openrouter_chat_with_retry",
        AsyncMock(side_effect=_http_error(429)),
    )

    for _ in range(generation.FALLBACK_STREAK_THRESHOLD):
        assert await generation.generate(prompt="extract facts") == "local result"
