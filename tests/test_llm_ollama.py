"""Tests for robothor.llm.ollama — config, constants, and retry behavior."""

from __future__ import annotations

import httpx
import pytest

from robothor.llm import ollama
from robothor.llm.ollama import (
    GENERATION_MODEL,
    GENERATION_MODEL_PREFERENCES,
    chat,
    get_embedding_async,
)


class TestModelConfig:
    def test_default_generation_model(self):
        assert GENERATION_MODEL is not None
        assert isinstance(GENERATION_MODEL, str)

    def test_model_preferences_ordered(self):
        assert len(GENERATION_MODEL_PREFERENCES) >= 2
        # First preference should be a known local generation model
        first = GENERATION_MODEL_PREFERENCES[0]
        assert isinstance(first, str) and len(first) > 0

    def test_all_preferences_are_strings(self):
        for pref in GENERATION_MODEL_PREFERENCES:
            assert isinstance(pref, str)


@pytest.fixture(autouse=True)
def _reset_transport():
    """Ensure the test transport seam is always reset, even on failure."""
    yield
    ollama._transport = None


def _install_transport(handler):
    ollama._transport = httpx.MockTransport(handler)


def _chat_response(content: str = "hi") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"content": content, "thinking": ""},
            "done_reason": "stop",
            "eval_count": 3,
        },
    )


def _embed_response(vector: list[float] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"embeddings": [vector or [0.1, 0.2, 0.3]]})


class TestChatRetry:
    async def test_chat_actually_retries_twice(self):
        """A 503 on the first attempt must be retried — and attempt 2 must succeed."""
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="server busy")
            return _chat_response("recovered")

        _install_transport(handler)

        result = await chat(messages=[{"role": "user", "content": "hi"}])

        assert result == "recovered"
        assert calls["n"] == 2

    async def test_chat_does_not_retry_400(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, text="bad request")

        _install_transport(handler)

        with pytest.raises(httpx.HTTPStatusError):
            await chat(messages=[{"role": "user", "content": "hi"}])

        assert calls["n"] == 1

    async def test_no_sleep_after_final_attempt(self, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="always busy")

        _install_transport(handler)

        with pytest.raises(httpx.HTTPStatusError):
            await chat(messages=[{"role": "user", "content": "hi"}])

        # CHAT_MAX_ATTEMPTS attempts means (CHAT_MAX_ATTEMPTS - 1) sleeps —
        # never a sleep after the last, doomed attempt.
        assert len(sleeps) == ollama.CHAT_MAX_ATTEMPTS - 1


class TestEmbedRetry:
    async def test_embed_retries_503_within_budget(self, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, text="server busy")
            return _embed_response()

        _install_transport(handler)

        result = await get_embedding_async("some text")

        assert result == [0.1, 0.2, 0.3]
        assert calls["n"] == 3
        assert len(sleeps) == 2
        # Total sleep time must stay comfortably within the ~2 minute budget.
        assert sum(sleeps) < 120

    async def test_embed_does_not_retry_404(self):
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404, text="model not found")

        _install_transport(handler)

        with pytest.raises(httpx.HTTPStatusError):
            await get_embedding_async("some text")

        assert calls["n"] == 1

    async def test_embed_no_sleep_after_final_attempt(self, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="always busy")

        _install_transport(handler)

        with pytest.raises(httpx.HTTPStatusError):
            await get_embedding_async("some text")

        assert len(sleeps) == ollama.EMBED_MAX_ATTEMPTS - 1

    async def test_embed_honors_retry_after_header(self, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
            return _embed_response()

        _install_transport(handler)

        await get_embedding_async("some text")

        assert sleeps == [7.0]

    async def test_embed_retries_mid_stream_read_error(self, monkeypatch):
        """A connection reset mid-response (Ollama restarting) must be retried."""

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadError("connection reset by peer", request=request)
            return _embed_response()

        _install_transport(handler)

        result = await get_embedding_async("some text")

        assert result == [0.1, 0.2, 0.3]
        assert calls["n"] == 2

    async def test_chat_retries_remote_protocol_error(self, monkeypatch):
        """Server disconnect mid-response (RemoteProtocolError) must be retried."""

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("server disconnected", request=request)
            return _chat_response("recovered")

        _install_transport(handler)

        result = await chat(messages=[{"role": "user", "content": "hi"}])

        assert result == "recovered"
        assert calls["n"] == 2

    async def test_embed_malformed_retry_after_does_not_crash(self, monkeypatch):
        """A date-format or garbage Retry-After falls back to normal backoff."""
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(ollama.asyncio, "sleep", fake_sleep)

        calls = {"n": 0}
        headers = ["Wed, 21 Oct 2026 07:28:00 GMT", "nan"]

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= len(headers):
                return httpx.Response(
                    429,
                    headers={"Retry-After": headers[calls["n"] - 1]},
                    text="slow down",
                )
            return _embed_response()

        _install_transport(handler)

        result = await get_embedding_async("some text")

        assert result == [0.1, 0.2, 0.3]
        assert len(sleeps) == 2
        for s in sleeps:
            # Backoff fallback: finite, positive, within the per-sleep cap
            # (+jitter). Never nan, never derived from the malformed header.
            assert s == s  # not nan
            assert 0 < s <= ollama.EMBED_BACKOFF_CAP_SECONDS * 1.25


class TestEmbedKeepAlive:
    def test_embed_keep_alive_is_pinned(self):
        from robothor.config import get_config

        # Must be a negative *duration with a unit*: Ollama parses string
        # keep_alive via Go time.ParseDuration, and a bare "-1" is a 400.
        assert get_config().ollama.keep_alive_embedding == "-1m"

    def test_all_keep_alive_values_are_valid_go_durations(self):
        """Ollama parses string keep_alive with Go time.ParseDuration.

        Every unit-less value except "0" (e.g. "-1", "15") is rejected with
        HTTP 400 "missing unit in duration" — which the retry classifier
        correctly treats as non-retryable, turning a config typo into a hard
        outage of the whole embed path. Enforce the grammar here.
        """
        import re

        from robothor.config import get_config

        go_duration = re.compile(r"^-?(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+$|^0$")
        cfg = get_config().ollama
        for field in (
            "keep_alive_embedding",
            "keep_alive_reranker",
            "keep_alive_generation",
            "keep_alive_vision",
        ):
            value = getattr(cfg, field)
            assert go_duration.match(value), (
                f"{field}={value!r} is not a valid Go duration; "
                'Ollama would reject it with HTTP 400 (use e.g. "-1m", "15m")'
            )

    async def test_embed_payload_sends_pinned_keep_alive(self):
        seen_payloads: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            seen_payloads.append(_json.loads(request.content))
            return _embed_response()

        _install_transport(handler)

        await get_embedding_async("some text")

        assert seen_payloads[0]["keep_alive"] == "-1m"
