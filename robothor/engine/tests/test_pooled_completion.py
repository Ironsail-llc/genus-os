"""The pooled wrapper injects a credential and retires a rejected one."""

from __future__ import annotations

import pytest

from robothor.engine import key_pool as kp
from robothor.engine.key_pool import Retirement
from robothor.engine.pooled_completion import acompletion


@pytest.fixture(autouse=True)
def _clean():
    kp.reset_shared_pools()
    yield
    kp.reset_shared_pools()


class _BoomError(Exception):
    def __init__(self, msg="", status=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


@pytest.mark.asyncio
async def test_the_pooled_key_is_injected(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr("litellm.acompletion", fake)
    await acompletion(model="openrouter/x", messages=[])
    assert seen["api_key"] == "sk-a"


@pytest.mark.asyncio
async def test_an_unpooled_provider_is_untouched(monkeypatch):
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr("litellm.acompletion", fake)
    await acompletion(model="ollama_chat/qwen", messages=[])
    assert "api_key" not in seen, "unpooled providers must behave exactly as before"


@pytest.mark.asyncio
async def test_an_explicit_key_wins(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr("litellm.acompletion", fake)
    await acompletion(model="openrouter/x", messages=[], api_key="sk-explicit")
    assert seen["api_key"] == "sk-explicit"


@pytest.mark.asyncio
async def test_a_weekly_cap_retires_as_periodic(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")

    async def fake(**kw):
        raise _BoomError("Key limit exceeded (weekly limit)")

    monkeypatch.setattr("litellm.acompletion", fake)
    with pytest.raises(_BoomError):
        await acompletion(model="openrouter/x", messages=[])
    pool = kp.shared_pool("OPENROUTER_API_KEY")
    assert pool is not None
    assert pool.status()[0].reason is Retirement.QUOTA_EXHAUSTED_PERIODIC


@pytest.mark.asyncio
async def test_a_model_specific_403_does_not_retire_the_key(monkeypatch):
    """A moderated model answering 403 must not burn a healthy credential."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")

    async def fake(**kw):
        raise _BoomError("Rate limited", status=403)

    monkeypatch.setattr("litellm.acompletion", fake)
    with pytest.raises(_BoomError):
        await acompletion(model="openrouter/x", messages=[])
    assert kp.api_key_for_model("openrouter/x") == "sk-a"


@pytest.mark.asyncio
async def test_a_rotation_is_visible_to_the_next_caller(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-a")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-b")

    async def fake(**kw):
        raise _BoomError("no auth", status=401)

    monkeypatch.setattr("litellm.acompletion", fake)
    with pytest.raises(_BoomError):
        await acompletion(model="openrouter/x", messages=[])
    assert kp.api_key_for_model("openrouter/x") == "sk-b"
