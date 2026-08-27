"""Tests for the autoDream generation-model unload gate.

The autoDream deep maintenance pass (run_lifecycle_maintenance) used to
unconditionally evict the generation model twice via Ollama's
``keep_alive: 0`` — a "free GPU pressure" dance that predates the current
unified-memory hardware (GB10, ~54GiB free at idle). On 2026-08-18 this
evicted models while concurrent chat traffic reloaded ~30GiB, causing the
embedding endpoint to fast-fail 503 over 300 times in 23 minutes.

These tests pin down the new behavior: the unload only fires under real
memory pressure (default threshold 24GiB, env-tunable), and it must never
target the embedding model.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from robothor.memory import lifecycle


def _mock_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json={},
        request=httpx.Request("POST", "https://ollama.test/api/generate"),
    )


def _mock_client() -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=_mock_response())
    return client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", raising=False)


class TestShouldUnloadGenerationModel:
    def test_skips_when_memory_plentiful(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 54.0)
        should_unload, available_gb, threshold_gb = lifecycle._should_unload_generation_model()
        assert should_unload is False
        assert available_gb == 54.0
        assert threshold_gb == 24.0

    def test_unloads_under_pressure(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 10.0)
        should_unload, available_gb, threshold_gb = lifecycle._should_unload_generation_model()
        assert should_unload is True
        assert available_gb == 10.0
        assert threshold_gb == 24.0

    def test_skips_when_memory_unknown(self, monkeypatch):
        # /proc/meminfo missing (non-Linux, sandboxed, etc.) — don't guess, skip.
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: None)
        should_unload, available_gb, threshold_gb = lifecycle._should_unload_generation_model()
        assert should_unload is False
        assert available_gb is None

    def test_threshold_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", "8")
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 10.0)
        should_unload, _, threshold_gb = lifecycle._should_unload_generation_model()
        # 10GiB available is above the lowered 8GiB threshold -> no unload.
        assert should_unload is False
        assert threshold_gb == 8.0

    def test_env_threshold_still_triggers_unload(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", "8")
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 5.0)
        should_unload, _, threshold_gb = lifecycle._should_unload_generation_model()
        assert should_unload is True
        assert threshold_gb == 8.0

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        # A bad env value must never crash the nightly deep pass — fall back
        # to the built-in default instead.
        monkeypatch.setenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", "lots")
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 10.0)
        should_unload, _, threshold_gb = lifecycle._should_unload_generation_model()
        assert threshold_gb == lifecycle._DEFAULT_UNLOAD_BELOW_GB
        assert should_unload is True

    def test_float_env_threshold_accepted(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", "12.5")
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 12.0)
        should_unload, _, threshold_gb = lifecycle._should_unload_generation_model()
        assert threshold_gb == 12.5
        assert should_unload is True

    def test_zero_env_threshold_disables_unload(self, monkeypatch):
        # Threshold 0 is a valid operator opt-out: available memory can never
        # be measurably below zero, so the unload never fires.
        monkeypatch.setenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", "0")
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 0.5)
        should_unload, _, threshold_gb = lifecycle._should_unload_generation_model()
        assert threshold_gb == 0.0
        assert should_unload is False


class TestPerformGenerationModelUnload:
    async def test_unload_skipped_when_memory_free(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 54.0)
        client = _mock_client()
        with patch("robothor.memory.lifecycle.httpx.AsyncClient", return_value=client):
            await lifecycle._perform_generation_model_unload()
        client.post.assert_not_called()

    async def test_unload_runs_under_pressure(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 10.0)
        monkeypatch.setattr(lifecycle.asyncio, "sleep", AsyncMock())
        client = _mock_client()
        with patch("robothor.memory.lifecycle.httpx.AsyncClient", return_value=client):
            await lifecycle._perform_generation_model_unload()
        client.post.assert_called_once()
        _, kwargs = client.post.call_args
        assert kwargs["json"]["model"] == lifecycle.llm_client.GENERATION_MODEL
        assert kwargs["json"]["keep_alive"] == 0

    async def test_unload_never_targets_embedding_model(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 1.0)
        monkeypatch.setattr(lifecycle.asyncio, "sleep", AsyncMock())
        client = _mock_client()
        # Simulate misconfiguration: generation model name collides with the
        # embedding model. The unload must refuse (logged skip) rather than
        # evict the (protected) embedding model — and must NOT raise, because
        # a crash here would kill the whole nightly deep pass.
        monkeypatch.setattr(
            lifecycle.llm_client, "GENERATION_MODEL", lifecycle.llm_client._embedding_model()
        )
        with patch("robothor.memory.lifecycle.httpx.AsyncClient", return_value=client):
            await lifecycle._perform_generation_model_unload()
        client.post.assert_not_called()

    async def test_pause_only_happens_when_unload_runs(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 54.0)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(lifecycle.asyncio, "sleep", sleep_mock)
        client = _mock_client()
        with patch("robothor.memory.lifecycle.httpx.AsyncClient", return_value=client):
            await lifecycle._perform_generation_model_unload(pause_after_s=2.0)
        sleep_mock.assert_not_called()

    async def test_pause_runs_after_successful_unload(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 10.0)
        sleep_mock = AsyncMock()
        monkeypatch.setattr(lifecycle.asyncio, "sleep", sleep_mock)
        client = _mock_client()
        with patch("robothor.memory.lifecycle.httpx.AsyncClient", return_value=client):
            await lifecycle._perform_generation_model_unload(pause_after_s=2.0)
        sleep_mock.assert_called_once_with(2.0)


class TestAvailableMemoryGb:
    def test_reads_mem_available_from_proc_meminfo(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            "MemTotal:       65000000 kB\nMemFree:        30000000 kB\n"
            "MemAvailable:   50331648 kB\n"
        )
        monkeypatch.setattr(lifecycle, "_MEMINFO_PATH", str(meminfo))
        available = lifecycle._available_memory_gb()
        assert available == pytest.approx(48.0, abs=0.01)

    def test_returns_none_when_file_missing(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_MEMINFO_PATH", "/nonexistent/meminfo")
        assert lifecycle._available_memory_gb() is None

    def test_returns_none_when_mem_available_absent(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       65000000 kB\n")
        monkeypatch.setattr(lifecycle, "_MEMINFO_PATH", str(meminfo))
        assert lifecycle._available_memory_gb() is None


class TestItRefusesTheFleetsOfflineTier:
    """autoDream must never evict the model the whole fleet falls back to.

    The existing guard refuses only the EMBEDDING model. The fleet's
    last-resort tier — the one `config._with_last_resort` appends to every
    agent's chain — had no such protection, and autoDream's trigger is memory
    pressure, which is exactly the condition a resident 27B creates.

    Latent, not theoretical: GENERATION_MODEL defaults to qwen3:8b today, but
    `detect_generation_model()`'s final fallback matches "any qwen3 that is not
    an embedder or reranker", which matches qwen3.8:27b. One env var or one
    caller and autoDream would unload the fleet's only offline tier mid-outage,
    while every agent was depending on it.
    """

    def _force_pressure(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "_available_memory_gb", lambda: 1.0)
        monkeypatch.setenv("ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", "999")

    @pytest.mark.asyncio
    async def test_it_refuses_to_unload_the_last_resort_model(self, monkeypatch):
        self._force_pressure(monkeypatch)
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/qwen3.8:27b")
        monkeypatch.setattr(lifecycle.llm_client, "GENERATION_MODEL", "qwen3.8:27b")
        monkeypatch.setattr(
            lifecycle.llm_client, "_embedding_model", lambda: "qwen3-embedding:0.6b"
        )

        posted: list[Any] = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                posted.append((a, k))

        monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda *a, **k: _Client())
        await lifecycle._perform_generation_model_unload()
        assert not posted, "autoDream unloaded the fleet's offline tier"

    @pytest.mark.asyncio
    async def test_the_refusal_compares_canonically(self, monkeypatch):
        """The ollama_chat/ routing prefix must not defeat the guard."""
        self._force_pressure(monkeypatch)
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/qwen3.8:27b")
        # bare id on one side, routed id on the other
        monkeypatch.setattr(lifecycle.llm_client, "GENERATION_MODEL", "ollama_chat/qwen3.8:27b")
        monkeypatch.setattr(
            lifecycle.llm_client, "_embedding_model", lambda: "qwen3-embedding:0.6b"
        )

        posted: list[Any] = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                posted.append((a, k))

        monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda *a, **k: _Client())
        await lifecycle._perform_generation_model_unload()
        assert not posted

    @pytest.mark.asyncio
    async def test_an_unrelated_generation_model_still_unloads(self, monkeypatch):
        """The guard is a guard, not a disable — autoDream must still work."""
        self._force_pressure(monkeypatch)
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/qwen3.8:27b")
        monkeypatch.setattr(lifecycle.llm_client, "GENERATION_MODEL", "qwen3:1.7b")
        monkeypatch.setattr(
            lifecycle.llm_client, "_embedding_model", lambda: "qwen3-embedding:0.6b"
        )

        posted: list[Any] = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                posted.append((a, k))

        monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda *a, **k: _Client())
        await lifecycle._perform_generation_model_unload()
        assert posted, "the guard disabled autoDream entirely"
