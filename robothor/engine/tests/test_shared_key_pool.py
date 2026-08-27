"""One pool per provider per process, shared by every caller.

Before this, ``LLMClient`` cached pools on the instance and
``memory/generation`` kept its own module-level pool. Two pools for one
credential means retiring it in one place leaves the other still dialling
a key the provider has already rejected — which is what kept the 403s
flowing on 2026-08-27 after the engine's own pool had correctly given up.

A credential is a process-wide fact. Retiring it must be one too.
"""

from __future__ import annotations

import pytest

from robothor.engine import key_pool as kp
from robothor.engine.key_pool import Retirement


@pytest.fixture(autouse=True)
def _clean():
    kp.reset_shared_pools()
    yield
    kp.reset_shared_pools()


def test_the_same_var_yields_the_same_pool(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    assert kp.shared_pool("OPENROUTER_API_KEY") is kp.shared_pool("OPENROUTER_API_KEY")


def test_a_retirement_is_visible_to_every_caller(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-b")

    caller_one = kp.shared_pool("OPENROUTER_API_KEY")
    caller_two = kp.shared_pool("OPENROUTER_API_KEY")
    assert caller_one is not None and caller_two is not None

    caller_one.retire("sk-a", Retirement.AUTH_FAILED)

    assert caller_two.current() == "sk-b", (
        "a second caller is still holding the retired credential — the pool "
        "is not a process-wide chokepoint"
    )


def test_an_unconfigured_provider_has_no_pool(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_2", raising=False)
    assert kp.shared_pool("OPENROUTER_API_KEY") is None


def test_a_model_resolves_to_its_provider_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    assert kp.api_key_for_model("openrouter/xiaomi/mimo-v2.5") == "sk-a"


def test_an_unpooled_model_resolves_to_nothing(monkeypatch):
    """Absent provider = litellm's own env resolution, today's behaviour."""
    assert kp.api_key_for_model("ollama_chat/qwen3.8:27b") is None


def test_an_exhausted_provider_resolves_to_nothing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    monkeypatch.delenv("OPENROUTER_API_KEY_2", raising=False)
    kp.retire_for_model("openrouter/x", "sk-a", Retirement.AUTH_FAILED)
    assert kp.api_key_for_model("openrouter/x") is None


def test_retiring_an_unpooled_model_is_a_no_op():
    kp.retire_for_model("ollama_chat/local", "whatever", Retirement.AUTH_FAILED)
