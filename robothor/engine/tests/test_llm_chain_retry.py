"""Fallback-chain resilience: transient failures get one in-place retry.

The 2026-08-20 email-classifier outage exhausted the whole 3-model OpenRouter
chain in ~6 minutes because each model got exactly one 120s attempt: a
transient 502 or a slow generation on 2-3 models killed the run. These tests
pin the fix:

- one retry with short jitter per model for timeouts/5xx before advancing;
- non-transient errors (e.g. HTTP 400) advance immediately, no retry;
- the exhaustion log carries ``repr(last_error)`` so a blank
  ``str(TimeoutError())`` can no longer produce ``last error:`` (empty);
- the per-request timeout is env-tunable, with a higher batch value for
  non-interactive (cron/workflow) runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


@pytest.fixture(autouse=True)
def _no_jitter_and_isolated_breaker(monkeypatch):
    """Zero the retry jitter and give each test a fresh, alert-free breaker."""
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    fresh = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)


def _err(status: int) -> Exception:
    e = Exception(f"HTTP {status}")
    e.status_code = status
    return e


# ─── retry-before-advance ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transient_502_retries_same_model_then_succeeds(client):
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(502), ok])
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}],
            ["openrouter/primary", "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert result is ok
    assert acompletion.call_count == 2
    # Both attempts went to the SAME model — the chain did not advance.
    models_called = [c.kwargs["model"] for c in acompletion.call_args_list]
    assert models_called == ["openrouter/primary", "openrouter/primary"]


@pytest.mark.asyncio
async def test_timeout_retries_once_then_advances(client):
    ok = object()
    acompletion = AsyncMock(side_effect=[TimeoutError(), TimeoutError(), ok])
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}],
            ["openrouter/primary", "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert result is ok
    # Two attempts on primary (initial + retry), then one on fallback.
    models_called = [c.kwargs["model"] for c in acompletion.call_args_list]
    assert models_called == [
        "openrouter/primary",
        "openrouter/primary",
        "openrouter/fallback",
    ]


@pytest.mark.asyncio
async def test_non_transient_400_does_not_retry(client):
    ok = object()
    acompletion = AsyncMock(side_effect=[_err(400), ok])
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}],
            ["openrouter/primary", "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert result is ok
    models_called = [c.kwargs["model"] for c in acompletion.call_args_list]
    # 400 is a permanent request error: advance immediately, no second dial.
    assert models_called == ["openrouter/primary", "openrouter/fallback"]


# ─── exhaustion logging ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exhaustion_log_shows_repr_of_blank_timeout(client, caplog):
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=TimeoutError()),
        ),
        caplog.at_level("ERROR", logger="robothor.engine.llm_client"),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}],
            ["openrouter/only"],
            [],
            broken_models=set(),
        )
    assert result is None
    exhausted = [r.getMessage() for r in caplog.records if "All models failed" in r.getMessage()]
    assert exhausted, "chain exhaustion must be logged at ERROR"
    # str(TimeoutError()) == "" — the log must use repr so the cause is visible.
    assert "TimeoutError" in exhausted[-1]


# ─── env-tunable timeouts ───────────────────────────────────────────────


def test_timeout_env_helper_reads_env(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_LLM_TIMEOUT_X_TEST", "77")
    assert llm_client._timeout_from_env("ROBOTHOR_LLM_TIMEOUT_X_TEST", 120) == 77


def test_timeout_env_helper_default_and_garbage(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_LLM_TIMEOUT_X_TEST", raising=False)
    assert llm_client._timeout_from_env("ROBOTHOR_LLM_TIMEOUT_X_TEST", 120) == 120
    monkeypatch.setenv("ROBOTHOR_LLM_TIMEOUT_X_TEST", "not-a-number")
    assert llm_client._timeout_from_env("ROBOTHOR_LLM_TIMEOUT_X_TEST", 120) == 120


def test_timeout_defaults():
    assert llm_client.LLM_REQUEST_TIMEOUT == 120
    assert llm_client.LLM_REQUEST_TIMEOUT_BATCH == 300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [
        TriggerType.CRON,
        TriggerType.WORKFLOW,
        # Added 2026-09-13 (DIAG 4.3): `event` and `sub_agent` carried 76 of
        # the 86 timeout-straddling steps in a two-day window — an inbound-mail
        # classification and a spawned sub-agent have no human on the other end
        # and are exactly as batch-shaped as a cron, but were capped at 120s.
        TriggerType.EVENT,
        TriggerType.SUB_AGENT,
    ],
)
async def test_batch_triggers_get_batch_timeout(client, trigger):
    session = AgentSession(agent_id="test-agent", trigger_type=trigger)
    inner = AsyncMock(return_value=None)
    with patch.object(LLMClient, "_call_llm", new=inner):
        await client._do_llm_call(session, ["openrouter/m"], [], None, set(), 0.3)
    assert inner.call_args.kwargs["timeout_override"] == float(llm_client.LLM_REQUEST_TIMEOUT_BATCH)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [TriggerType.TELEGRAM, TriggerType.WEBCHAT, TriggerType.SLACK])
async def test_interactive_trigger_keeps_default_timeout(client, trigger):
    """A human is waiting on these: 120s, unchanged."""
    session = AgentSession(agent_id="test-agent", trigger_type=trigger)
    inner = AsyncMock(return_value=None)
    with patch.object(LLMClient, "_call_llm", new=inner):
        await client._do_llm_call(session, ["openrouter/m"], [], None, set(), 0.3)
    assert inner.call_args.kwargs["timeout_override"] is None


#: The WHOLE trigger→timeout table, batch and interactive alike. Parametrising
#: only the two sets under discussion left `hook`, `manual`, `webhook`,
#: `federation`, `channel_event` and `ide` at 120s by OMISSION — and two of
#: those are arguably as unattended as `event` (review M9). Frozen here so the
#: next change to the set is a decision, not a side effect.
_TRIGGER_TIMEOUTS = {
    TriggerType.CRON: "batch",
    TriggerType.WORKFLOW: "batch",
    TriggerType.EVENT: "batch",
    TriggerType.SUB_AGENT: "batch",
    TriggerType.TELEGRAM: "interactive",
    TriggerType.WEBCHAT: "interactive",
    TriggerType.SLACK: "interactive",
    TriggerType.CHANNEL_EVENT: "interactive",
    TriggerType.IDE: "interactive",
    TriggerType.MANUAL: "interactive",
    # Not yet batch-shaped, deliberately: a hook and a webhook can both be a
    # human waiting on a reply, and federation is another instance's turn.
    TriggerType.HOOK: "interactive",
    TriggerType.WEBHOOK: "interactive",
    TriggerType.FEDERATION: "interactive",
}


def test_the_trigger_timeout_table_covers_every_trigger():
    """A new TriggerType must be classified, not inherit 120s by omission."""
    assert set(_TRIGGER_TIMEOUTS) == set(TriggerType)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger,kind", sorted(_TRIGGER_TIMEOUTS.items()))
async def test_every_trigger_gets_the_timeout_the_table_pins(client, trigger, kind):
    session = AgentSession(agent_id="test-agent", trigger_type=trigger)
    inner = AsyncMock(return_value=None)
    with patch.object(LLMClient, "_call_llm", new=inner):
        await client._do_llm_call(session, ["openrouter/m"], [], None, set(), 0.3)
    override = inner.call_args.kwargs["timeout_override"]
    if kind == "batch":
        assert override == float(llm_client.LLM_REQUEST_TIMEOUT_BATCH)
    else:
        assert override is None


# ─── one allowance per model, shared by its attempts (review I6) ────────


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_a_model_that_spends_its_allowance_gets_no_second_attempt(client):
    """Three 300s attempts per model x a 4-model chain is an hour per dispatch.

    The run's wall-clock ceiling is only checked BETWEEN loop iterations, so a
    single dispatch could overshoot it by that whole amount — and
    `thread_pool.PENDING_EXPIRY_SECONDS` (1800) sits below it.
    """
    clock = _Clock()
    ok = object()
    calls: list[str] = []

    async def _acompletion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "openrouter/primary":
            clock.t += float(llm_client.LLM_REQUEST_TIMEOUT)  # the whole allowance
            raise TimeoutError()
        return ok

    with (
        patch("time.monotonic", clock),
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_acompletion),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}],
            ["openrouter/primary", "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert result is ok
    assert calls == ["openrouter/primary", "openrouter/fallback"], (
        "a model that spent its whole allowance on one call must not get a "
        "second one — that doubling is what put a dispatch past 1800s"
    )


@pytest.mark.asyncio
async def test_a_fast_failure_still_gets_its_in_place_retry(client):
    """The 2026-08-20 fix stands: a blip must not burn the model's attempt."""
    clock = _Clock()
    ok = object()
    calls: list[str] = []

    async def _acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            clock.t += 2.0  # a 502 in two seconds leaves the allowance intact
            raise _err(502)
        return ok

    with (
        patch("time.monotonic", clock),
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_acompletion),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}],
            ["openrouter/primary", "openrouter/fallback"],
            [],
            broken_models=set(),
        )
    assert result is ok
    assert calls == ["openrouter/primary", "openrouter/primary"]


def test_the_worst_case_dispatch_fits_the_pending_expiry_window():
    """The arithmetic, stated: one dispatch must fit inside 1800s."""
    from robothor.engine.llm_client import (
        MAX_CHAIN_MODELS_BUDGETED,
        worst_case_dispatch_seconds,
    )
    from robothor.engine.thread_pool import PENDING_EXPIRY_SECONDS

    worst = worst_case_dispatch_seconds(
        MAX_CHAIN_MODELS_BUDGETED, llm_client.LLM_REQUEST_TIMEOUT_BATCH
    )
    assert worst < PENDING_EXPIRY_SECONDS, (
        f"a batch dispatch can outlast the window a sub-agent turn fits in: "
        f"{worst}s vs {PENDING_EXPIRY_SECONDS}s"
    )
    # Without the shared allowance it is three attempts per model, which does
    # NOT fit — that is the arithmetic this pins.
    unshared = MAX_CHAIN_MODELS_BUDGETED * 3 * llm_client.LLM_REQUEST_TIMEOUT_BATCH
    assert unshared > PENDING_EXPIRY_SECONDS
