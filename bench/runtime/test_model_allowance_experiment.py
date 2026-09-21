"""An interactive latency experiment must not disable providers for goal work."""

import pytest

from bench.runtime.model_allowance_experiment import install
from robothor.engine import llm_client
from robothor.engine.model_breaker import ModelBreaker


@pytest.mark.parametrize("isolated", [False, True])
def test_timeout_health_is_separate_but_real_failures_still_open_breaker(monkeypatch, isolated):
    original_timeout, original_blame = llm_client._per_call_timeout, llm_client._blame_model
    breaker = ModelBreaker(threshold=2)
    with monkeypatch.context() as scoped:
        install(scoped, 8, isolate_short_timeout_health=isolated)
        assert llm_client._per_call_timeout("openrouter/test/model", None) == 8
        assert llm_client._per_call_timeout("openrouter/test/model", 3) == 3
        for _ in range(2):
            llm_client._blame_model(breaker, TimeoutError(), "short-window", 8)
        assert breaker.is_open("short-window") is not isolated
        for _ in range(2):
            llm_client._blame_model(breaker, RuntimeError("provider unavailable"), "failed", 8)
        assert breaker.is_open("failed")
    assert llm_client._per_call_timeout is original_timeout
    assert llm_client._blame_model is original_blame


def test_cloud_allowance_preserves_local_timeout_and_local_failure_policy(monkeypatch):
    model = "ollama_chat/qwen3.8:27b"
    original = llm_client._per_call_timeout(model, None)
    breaker = ModelBreaker(threshold=1)
    install(monkeypatch, 10, isolate_short_timeout_health=True, cloud_only=True)
    assert llm_client._per_call_timeout(model, None) == original
    assert llm_client._per_call_timeout("openrouter/test/model", None) == 10
    llm_client._blame_model(breaker, TimeoutError(), model, original)
    assert breaker.is_open(model)
    llm_client._blame_model(breaker, TimeoutError(), "openrouter/test/model", 10)
    assert not breaker.is_open("openrouter/test/model")
