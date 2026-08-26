"""The pool is wired into the call path, not merely importable.

On 2026-08-25 one capped OpenRouter key stopped the whole fleet: every model
in every fallback chain authenticates with the same credential, so a four-deep
chain failed four identical ways in the same instant.

These tests assert on the api_key each call actually carried. A test that the
pool is *consulted* would pass against a client that consults it and then
sends the original key anyway — the failure mode that shipped #407 inert.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.key_pool import Retirement
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


@pytest.fixture(autouse=True)
def _no_jitter_and_isolated_breaker(monkeypatch):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: ModelBreaker(on_open=None))


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch):
    for n in ("", "_2", "_3", "_4"):
        monkeypatch.delenv(f"OPENROUTER_API_KEY{n}", raising=False)


def _err(status: int, message: str = "") -> Exception:
    e = Exception(message or f"HTTP {status}")
    e.status_code = status
    return e


def _keys_used(mock) -> list[str | None]:
    return [c.kwargs.get("api_key") for c in mock.call_args_list]


async def _run(client, acompletion, chain=("openrouter/primary",)):
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        return await client._call_llm(
            [{"role": "user", "content": "hi"}], list(chain), [], broken_models=set()
        )


@pytest.mark.asyncio
async def test_a_capped_key_rotates_to_the_spare_and_the_run_survives(client, monkeypatch):
    """The 2026-08-25 incident, with a spare key configured."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(402, "credit limit exceeded"), ok])

    result = await _run(client, acompletion)

    assert result is ok
    assert _keys_used(acompletion) == ["sk-primary", "sk-spare"]


@pytest.mark.asyncio
async def test_rotation_stays_on_the_same_model(client, monkeypatch):
    """A dead credential is not a dead model — the chain must not advance.

    Walking to the fallback would burn a model for a reason that had nothing
    to do with it, exactly as the rate-limit taxonomy fixed for 429s.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(402), ok])

    await _run(client, acompletion, chain=("openrouter/primary", "openrouter/fallback"))

    assert [c.kwargs["model"] for c in acompletion.call_args_list] == [
        "openrouter/primary",
        "openrouter/primary",
    ]


@pytest.mark.asyncio
async def test_a_single_capped_key_still_raises(client, monkeypatch):
    """With nothing to rotate to, behaviour is exactly what it is today."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-only")
    acompletion = AsyncMock(side_effect=_err(402, "credit limit exceeded"))

    with pytest.raises(Exception, match="credit"):
        await _run(client, acompletion)

    assert acompletion.call_count == 1


@pytest.mark.asyncio
async def test_all_keys_capped_raises_rather_than_looping(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    acompletion = AsyncMock(side_effect=_err(402, "credit limit exceeded"))

    with pytest.raises(Exception, match="credit"):
        await _run(client, acompletion)

    assert _keys_used(acompletion) == ["sk-primary", "sk-spare"]


@pytest.mark.asyncio
async def test_a_revoked_key_rotates_too(client, monkeypatch):
    """401 is a credential failure, not a model failure."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-revoked")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-good")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(401, "invalid api key"), ok])

    result = await _run(client, acompletion)

    assert result is ok
    assert _keys_used(acompletion) == ["sk-revoked", "sk-good"]


@pytest.mark.asyncio
async def test_a_retired_key_is_not_retried_later_in_the_same_run(client, monkeypatch):
    """Once retired, a key must not come back on the next model in the chain.

    Otherwise every model in a four-deep chain pays the dead credential's
    round trip before advancing — the tax this module exists to remove.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-dead")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-live")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(402), _err(500), ok])

    await _run(client, acompletion, chain=("openrouter/primary", "openrouter/fallback"))

    assert _keys_used(acompletion) == ["sk-dead", "sk-live", "sk-live"]


@pytest.mark.asyncio
async def test_no_key_configured_sends_no_api_key(client):
    """litellm's own env resolution must keep working when no pool exists.

    Passing api_key=None explicitly would override that and break every
    deployment that never sets a numbered sibling — i.e. all of them today.
    """
    ok = object()
    acompletion = AsyncMock(return_value=ok)

    await _run(client, acompletion)

    assert "api_key" not in acompletion.call_args_list[0].kwargs


# ─── a spent key must not strand a keyless local tier ───────────────────
#
# The 2026-08-26 chat outage. main's chain ends in ollama_chat/qwen3.8:27b,
# which needs no credential and was sitting on the box answering in 9.8s —
# but a spent OpenRouter key raised instead of advancing, so the chain never
# reached it. The local tier existed, worked, and was unreachable.
#
# Skipping the rest of the chain on credit exhaustion is right for models
# that share the dead credential. It is wrong for every model that does not.


@pytest.mark.asyncio
async def test_a_spent_key_still_falls_through_to_the_local_tier(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-capped")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(402, "credit limit exceeded"), ok])

    result = await _run(
        client, acompletion, chain=("openrouter/primary", "ollama_chat/qwen3.8:27b")
    )

    assert result is ok
    assert [c.kwargs["model"] for c in acompletion.call_args_list] == [
        "openrouter/primary",
        "ollama_chat/qwen3.8:27b",
    ]


@pytest.mark.asyncio
async def test_a_spent_key_skips_the_models_that_share_it(client, monkeypatch):
    """Walking siblings on the dead key wastes a round trip each and cannot work."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-capped")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(402, "credit limit exceeded"), ok])

    await _run(
        client,
        acompletion,
        chain=(
            "openrouter/primary",
            "openrouter/sibling-a",
            "openrouter/sibling-b",
            "ollama_chat/qwen3.8:27b",
        ),
    )

    assert [c.kwargs["model"] for c in acompletion.call_args_list] == [
        "openrouter/primary",
        "ollama_chat/qwen3.8:27b",
    ], "the two siblings share the spent key and must be skipped, not tried"


@pytest.mark.asyncio
async def test_with_nothing_keyless_left_it_still_raises_the_credit_error(client, monkeypatch):
    """A clear 'top up the account' beats a bare None when nothing can run."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-capped")
    acompletion = AsyncMock(side_effect=_err(402, "credit limit exceeded"))

    with pytest.raises(Exception, match="credit"):
        await _run(client, acompletion, chain=("openrouter/primary", "openrouter/sibling"))

    assert acompletion.call_count == 1


# ─── credential failures are not model failures ─────────────────────────
#
# Found by live probe, not by a mock: firing a real 402 at the pool showed
# the run ending with `broken: {'openrouter/...'}`. A dead key was being
# attributed to the model — so a chain of four models blacklists all four,
# one credential round-trip at a time, and the circuit breaker then keeps
# skipping them after the key is topped up. Exactly the misattribution #410
# fixed for 429s, wearing a different status code.


@pytest.mark.asyncio
async def test_a_credential_failure_does_not_mark_the_model_broken(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-only")
    broken: set[str] = set()
    acompletion = AsyncMock(side_effect=_err(401, "invalid api key"))

    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        await client._call_llm(
            [{"role": "user", "content": "hi"}], ["openrouter/primary"], [], broken_models=broken
        )

    assert broken == set(), "a rejected key says nothing about the model"


@pytest.mark.asyncio
async def test_a_credential_failure_does_not_trip_the_circuit_breaker(client, monkeypatch):
    """Otherwise the model stays in cooldown after the key is fixed."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-only")
    breaker = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: breaker)
    acompletion = AsyncMock(side_effect=_err(402, "credit limit exceeded"))

    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        pytest.raises(Exception, match="credit"),
    ):
        await client._call_llm(
            [{"role": "user", "content": "hi"}], ["openrouter/primary"], [], broken_models=set()
        )

    assert not breaker.is_open("openrouter/primary")


@pytest.mark.asyncio
async def test_a_403_is_model_access_not_a_dead_key(client, monkeypatch):
    """403 must neither retire the key nor rotate — it is model-specific.

    OpenRouter answers 403 for "this key may not use *this model*" (privileged,
    moderated, or region-blocked models). Widening the credential check to
    include it would permanently retire a working key over one model, and skip
    the model the operator actually needs told about. This test exists to stop
    that widening.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-good")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    broken: set[str] = set()
    acompletion = AsyncMock(side_effect=_err(403, "model requires a paid account"))

    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        await client._call_llm(
            [{"role": "user", "content": "hi"}], ["openrouter/primary"], [], broken_models=broken
        )

    assert _keys_used(acompletion) == ["sk-good"], "no rotation on a model-access denial"
    assert "openrouter/primary" in broken, "the model, not the key, is the problem"


@pytest.mark.asyncio
async def test_a_genuine_model_failure_still_marks_it_broken(client, monkeypatch):
    """The guard must be narrow — a 500 is still the model's problem."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-only")
    broken: set[str] = set()
    acompletion = AsyncMock(side_effect=_err(500))

    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        await client._call_llm(
            [{"role": "user", "content": "hi"}], ["openrouter/primary"], [], broken_models=broken
        )

    assert "openrouter/primary" in broken


@pytest.mark.asyncio
async def test_an_ordinary_failure_does_not_retire_a_key(client, monkeypatch):
    """A 500 is the provider's problem. Burning a credential over it would
    empty the pool during an outage that had nothing to do with credentials."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(500), ok])

    await _run(client, acompletion)

    assert _keys_used(acompletion) == ["sk-primary", "sk-primary"]


# ─── defects found by adversarial review of this branch ──────────────────
#
# Every test below reproduces a defect that a review agent found and an
# independent verifier confirmed empirically against this branch. Two of them
# would have created NEW outage modes in the code written to end an outage.


@pytest.mark.asyncio
async def test_retires_the_key_the_request_used_not_whatever_is_current(client, monkeypatch):
    """A concurrent failure must not retire the healthy key that replaced it.

    The daemon shares one LLMClient — and so one KeyPool — across every
    concurrent run. Two calls go out on sk-primary; it is capped; both come
    back 402. The first retires sk-primary and rotates. The second must retire
    the key IT used, not re-read the pool and burn sk-spare, which just
    answered successfully.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-primary")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    pool = client._key_pool("openrouter/primary")
    ok = object()
    calls: list[str | None] = []

    async def side_effect(**kw):
        calls.append(kw.get("api_key"))
        if len(calls) == 1:
            # A concurrent run already retired the capped key and moved on.
            pool.retire("sk-primary", Retirement.CREDIT_EXHAUSTED)
            raise _err(402, "credit limit exceeded")
        return ok

    acompletion = AsyncMock(side_effect=side_effect)
    await _run(client, acompletion)

    spare = [s for s in pool.status() if s.position == 2][0]
    assert spare.available, "the spare answered successfully and must stay in rotation"


@pytest.mark.asyncio
async def test_a_keyless_model_is_never_skipped_for_someone_elses_dead_key(client, monkeypatch):
    """A quota error from an unpooled provider must not strand the local tier.

    env_var_for_model returns None for everything that is not openrouter/, so
    recording None as a dead credential marks EVERY keyless model — the local
    ollama tier included — as sharing a spent key. That is the 2026-08-26
    outage reproduced through a different door.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
    ok = object()
    acompletion = AsyncMock(
        side_effect=[
            Exception("You exceeded your current quota, please check your plan"),
            ok,
        ]
    )

    result = await _run(
        client, acompletion, chain=("gemini/gemini-2.5-pro", "ollama_chat/qwen3.8:27b")
    )

    assert result is ok
    assert [c.kwargs["model"] for c in acompletion.call_args_list] == [
        "gemini/gemini-2.5-pro",
        "ollama_chat/qwen3.8:27b",
    ]


@pytest.mark.asyncio
async def test_an_exhausted_pool_does_not_leak_back_to_the_env_key(client, monkeypatch):
    """Retirement must not be undone by litellm's own env lookup.

    Omitting api_key once every key is retired hands litellm the very
    credential the pool just proved dead, so "out for the life of the process"
    is unenforceable exactly when it matters.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-revoked")
    pool = client._key_pool("openrouter/primary")
    pool.retire("sk-revoked", Retirement.AUTH_FAILED)
    ok = object()
    acompletion = AsyncMock(return_value=ok)

    result = await _run(
        client, acompletion, chain=("openrouter/primary", "ollama_chat/qwen3.8:27b")
    )

    assert result is ok
    models = [c.kwargs["model"] for c in acompletion.call_args_list]
    assert models == ["ollama_chat/qwen3.8:27b"], "a model whose only key is dead must be skipped"


@pytest.mark.asyncio
async def test_the_key_is_redacted_in_repr_so_tracebacks_cannot_print_it(client, monkeypatch):
    """structlog's console renderer dumps frame locals with show_locals=True.

    The engine binds the credential into _call_llm's frame and re-raises from
    it, so a rendered traceback would print the whole key. repr() is what that
    renderer calls, so repr is where the redaction has to live.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-0123456789abcdef")
    pool = client._key_pool("openrouter/primary")
    key = pool.current()

    assert key == "sk-or-v1-0123456789abcdef", "it must still work as the real credential"
    assert "0123456789abcdef" not in repr(key)
    assert "0123456789abcdef" not in repr({"api_key": key})


@pytest.mark.asyncio
async def test_spare_keys_do_not_inflate_the_transient_retry_budget(client, monkeypatch):
    """Rotation headroom must not be spendable on 5xx.

    Otherwise every configured spare buys one more full-timeout attempt
    against a hung provider on every model — so the operational fix this
    change recommends makes unrelated blips slower.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1")
    one = AsyncMock(side_effect=_err(503))
    await _run(client, one)
    baseline = one.call_count

    fresh = LLMClient()
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-2")
    monkeypatch.setenv("OPENROUTER_API_KEY_3", "sk-3")
    many = AsyncMock(side_effect=_err(503))
    await _run(fresh, many)

    assert many.call_count == baseline, "spare keys must not buy extra 5xx retries"


@pytest.mark.asyncio
async def test_a_duplicated_model_still_raises_the_credit_error(client, monkeypatch):
    """The fallthrough must use the loop's position, not the first index.

    With a model repeated in the chain, models.index() points behind the
    cursor, so the code breaks as though a fallback existed and the caller
    gets a bare None instead of the actionable credit error.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-capped")
    acompletion = AsyncMock(side_effect=_err(402, "credit limit exceeded"))

    with pytest.raises(Exception, match="credit"):
        await _run(client, acompletion, chain=("openrouter/p", "openrouter/p"))


@pytest.mark.asyncio
async def test_streaming_rotates_credentials_too(client, monkeypatch):
    """Interactive chat streams, so a pool that only helps cron is half a fix."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-capped")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-spare")
    seen: list[str | None] = []

    async def side_effect(**kw):
        seen.append(kw.get("api_key"))
        raise _err(402, "credit limit exceeded")

    acompletion = AsyncMock(side_effect=side_effect)
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        with contextlib.suppress(Exception):
            await client._call_llm_streaming(
                [{"role": "user", "content": "hi"}], ["openrouter/primary"], [], broken_models=set()
            )

    assert seen[:2] == ["sk-capped", "sk-spare"], f"streaming did not rotate: {seen}"
