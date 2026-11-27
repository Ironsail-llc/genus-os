"""Hosted benchmark calls authenticate through the shared credential pool.

The pool is the one door to a provider: a credential it has retired must not
keep being dialled, and a spare must be able to take over. The benchmark's
hosted client asks the pool for the model's key, retires one the provider
rejects, and never writes a key into a report or a cache file.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_hosted as eh
from robothor.memory.tests.test_eval_bench import _bench, _EchoSystem, _fake_ingest

KEY_1 = "sk-test-pool-key-one-0000000000"
KEY_2 = "sk-test-pool-key-two-0000000000"


def _response(text: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
    )


class _RejectedError(Exception):
    status_code = 401


@pytest.fixture
def pool(monkeypatch):
    from robothor.engine import key_pool

    monkeypatch.setenv("OPENAI_API_KEY", KEY_1)
    monkeypatch.setenv("OPENAI_API_KEY_2", KEY_2)
    key_pool.reset_shared_pools()
    yield key_pool
    key_pool.reset_shared_pools()


@pytest.fixture
def litellm_calls(monkeypatch):
    import litellm

    calls: list[dict[str, Any]] = []
    replies: list[Any] = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        reply = replies.pop(0) if replies else _response("yes")
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.mark.asyncio
async def test_the_key_comes_from_the_pool(pool, litellm_calls):
    await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    (call,) = litellm_calls.calls
    assert call["api_key"] == pool.api_key_for_model("openai/gpt-4o")


@pytest.mark.asyncio
async def test_a_rejected_key_is_retired_and_the_next_call_rotates(pool, litellm_calls):
    litellm_calls.replies.append(_RejectedError("invalid api key"))
    with pytest.raises(_RejectedError):
        await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    first, second = litellm_calls.calls
    assert first["api_key"] != second["api_key"], "the rejected key is not dialled again"
    assert second["api_key"] == pool.api_key_for_model("openai/gpt-4o")


@pytest.mark.asyncio
async def test_no_key_reaches_a_report_or_a_cache_file(pool, litellm_calls, tmp_path):
    from robothor.memory import eval_systems as es

    es.register_system("echo_bench", _EchoSystem)
    try:
        report = await eb.run_benchmark(
            _bench(1),
            systems=["echo_bench"],
            answer_model="openai/gpt-4o",
            judge_model="openai/gpt-4o-2024-08-06",
            cache_dir=str(tmp_path / "cache"),
            progress_path=str(tmp_path / "progress.jsonl"),
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            validate_judge=False,
        )
    finally:
        es.unregister_system("echo_bench")
    assert litellm_calls.calls, "the hosted client was used"
    written = [p.read_text(encoding="utf-8") for p in Path(tmp_path).rglob("*") if p.is_file()]
    written.append(eb.format_bench_markdown(report))
    for text in written:
        assert KEY_1 not in text and KEY_2 not in text


class _ModelRefusedError(Exception):
    """A 403: this key may not use this model; the key itself is fine."""

    status_code = 403


class _OutOfCreditError(Exception):
    """OpenAI's out-of-credit answer: a 429 that is not a rate limit."""

    status_code = 429


@pytest.mark.asyncio
async def test_a_model_specific_refusal_keeps_the_key(pool, litellm_calls):
    litellm_calls.replies.append(_ModelRefusedError("model not available for this org"))
    with pytest.raises(_ModelRefusedError):
        await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    first, second = litellm_calls.calls
    assert first["api_key"] == second["api_key"], "a 403 is the model's, not the key's"


@pytest.mark.asyncio
async def test_an_out_of_credit_429_rotates_instead_of_backing_off(
    pool, litellm_calls, monkeypatch
):
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(eh, "_sleep", _sleep)
    litellm_calls.replies.append(
        _OutOfCreditError("You exceeded your current quota (insufficient_quota)")
    )
    with pytest.raises(_OutOfCreditError):
        await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    assert slept == [], "no back-off: waiting does not refill a spent account"
    await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    first, second = litellm_calls.calls
    assert first["api_key"] != second["api_key"], "the spent key is retired"


@pytest.mark.asyncio
async def test_out_of_credit_with_no_pooled_key_raises_without_back_off(litellm_calls, monkeypatch):
    from robothor.engine import key_pool

    for name in ("OPENAI_API_KEY", "OPENAI_API_KEY_2"):
        monkeypatch.delenv(name, raising=False)
    key_pool.reset_shared_pools()
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(eh, "_sleep", _sleep)
    litellm_calls.replies.append(
        _OutOfCreditError("You exceeded your current quota (insufficient_quota)")
    )
    try:
        with pytest.raises(_OutOfCreditError):
            await eh.hosted_chat([{"role": "user", "content": "q"}], model="openai/gpt-4o")
    finally:
        key_pool.reset_shared_pools()
    assert slept == [] and len(litellm_calls.calls) == 1, "a spent account is not retried"
