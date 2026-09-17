"""Keeping the message list inside budget, before each LLM call.

Two steps that share one job and one failure mode. Extracted from `_run_loop`
as part of decomposing the god-object the competitive analysis puts first.

The behaviour worth pinning, none of which was asserted while this lived inline:

* it sizes against the model that will ACTUALLY be tried next (the first
  non-broken one), not the configured primary. That is G2b: a run on a
  smaller-window fallback compacting at the primary's larger threshold can
  overflow the fallback outright.
* it runs EVERY iteration. It used to run every fifth, and at ~10K tokens an
  iteration a five-gap overshoots the budget by half the budget again before
  anything looks.
* it never raises. A failure here must degrade to a warning: losing compaction
  costs money, and taking the run down with it costs the work.

The estimator these tests patch is ``context_fit.estimate_tokens``, not
``context.estimate_tokens``: the budget is sized through ``estimate_for``,
which prices token-dense content at what it actually costs rather than at a
flat four characters a token. Patching the other one leaves the real estimator
running and the test silently measures nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from robothor.engine.context_budget import keep_context_within_budget


def _session(messages=None):
    return SimpleNamespace(
        messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
        run_id="run-1",
        thinned=[],
        thin_previous_tool_results=lambda protect_after_index=0: 0,
    )


def _config(eager=False):
    return SimpleNamespace(id="probe", eager_tool_compression=eager)


async def _run(session, config, *, iteration=1, models=None, broken=None, hooks=None):
    await keep_context_within_budget(
        session,
        config,
        iteration=iteration,
        models=models or ["primary", "fallback"],
        broken_models=broken or set(),
        hook_registry=hooks,
        pre_iteration_msg_idx=0,
    )


# ── The first iteration has nothing to compact ────────────────────────


async def test_nothing_happens_on_the_first_iteration():
    session = _session()
    calls = []
    session.thin_previous_tool_results = lambda protect_after_index=0: calls.append(1) or 0

    with patch("robothor.engine.context.maybe_compress", new=AsyncMock()) as compress:
        await _run(session, _config(eager=True), iteration=0)

    assert calls == [] and not compress.called


# ── Eager thinning ────────────────────────────────────────────────────


async def test_eager_thinning_runs_when_the_agent_asked_for_it():
    session = _session()
    seen = []
    session.thin_previous_tool_results = lambda protect_after_index=0: (
        seen.append(protect_after_index) or 120
    )

    await _run(session, _config(eager=True))

    assert seen == [0]


async def test_eager_thinning_is_skipped_when_not_configured():
    session = _session()
    seen = []
    session.thin_previous_tool_results = lambda protect_after_index=0: seen.append(1) or 0

    await _run(session, _config(eager=False))

    assert seen == []


# ── Sizing: the G2b fix ───────────────────────────────────────────────


async def test_it_sizes_against_the_model_that_will_actually_answer():
    """The configured primary is down. Sizing against its 1M window while the
    run is on a 200K fallback is how a run overflows a model it never chose."""
    session = _session()

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10),
        patch("robothor.engine.context.maybe_compress", new=AsyncMock(return_value=[])),
        patch("robothor.engine.model_registry.get_model_limits") as limits,
    ):
        await _run(session, _config(), models=["primary", "fallback"], broken={"primary"})

    assert limits.call_args[0][0] == "fallback", (
        "sized against the configured primary while it was marked broken"
    )


# ── Compaction fires only above the threshold ─────────────────────────


async def test_a_small_context_is_left_alone():
    session = _session()

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10),
        patch("robothor.engine.context.maybe_compress", new=AsyncMock()) as compress,
    ):
        await _run(session, _config())

    assert not compress.called


async def test_a_large_context_is_compacted():
    session = _session()

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10_000_000),
        patch(
            "robothor.engine.context.maybe_compress",
            new=AsyncMock(return_value=[{"role": "user", "content": "smaller"}]),
        ) as compress,
    ):
        await _run(session, _config())

    assert compress.called
    assert session.messages[0] == {"role": "user", "content": "smaller"}
    # The estimator is pinned at ten million for the whole test, so the ceiling
    # can never be reached and the run is TOLD that plainly. A note claiming a
    # reduction that did not happen is the thing that was fixed here.
    assert "could not be cut down" in str(session.messages[-1]["content"])


# ── Hooks ─────────────────────────────────────────────────────────────


async def test_both_compaction_hooks_are_dispatched():
    from robothor.engine.hook_registry import HookEvent

    session = _session()
    hooks = SimpleNamespace(dispatch=AsyncMock())

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10_000_000),
        patch("robothor.engine.context.maybe_compress", new=AsyncMock(return_value=[])),
    ):
        await _run(session, _config(), hooks=hooks)

    events = [c.args[0] for c in hooks.dispatch.await_args_list]
    assert HookEvent.PRE_COMPACTION in events
    assert HookEvent.POST_COMPACTION in events


async def test_a_failing_hook_does_not_stop_the_compaction():
    """A third-party hook must not be able to disable compaction fleet-wide."""
    session = _session()
    hooks = SimpleNamespace(dispatch=AsyncMock(side_effect=RuntimeError("bad hook")))

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10_000_000),
        patch(
            "robothor.engine.context.maybe_compress",
            new=AsyncMock(return_value=[{"role": "user", "content": "smaller"}]),
        ) as compress,
    ):
        await _run(session, _config(), hooks=hooks)

    assert compress.called


# ── It must never take the run down ───────────────────────────────────


async def test_the_ceiling_is_enforced_even_when_compaction_fails():
    """The run that needs the ceiling most is the one where no model answers.

    Compaction summarises WITH a model. On 2026-09-16 every cloud model was
    unreachable, so the summariser would have walked the same dead chain — and
    a compaction failure that also skipped the ceiling is how oversized
    messages reach a server that truncates them in silence.
    """
    enforced = []
    session = _session([{"role": "user", "content": "x" * 2_000_000}])

    with (
        patch(
            "robothor.engine.context.maybe_compress",
            new=AsyncMock(side_effect=RuntimeError("no model could summarise")),
        ),
        patch(
            "robothor.engine.context_budget.enforce_hard_limit",
            side_effect=lambda s, fit: enforced.append(fit.model) or True,
        ),
    ):
        await _run(session, _config(), models=["ollama_chat/qwen3.8:27b"])

    assert enforced, "compaction failed and the ceiling was never applied"


async def test_a_compaction_failure_is_swallowed():
    """Losing compaction costs money. Losing the run costs the work."""
    session = _session()

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10_000_000),
        patch(
            "robothor.engine.context.maybe_compress",
            new=AsyncMock(side_effect=RuntimeError("compress exploded")),
        ),
    ):
        await _run(session, _config())  # must not raise


async def test_a_thinning_failure_does_not_prevent_compaction():
    session = _session()

    def _boom(protect_after_index=0):
        raise RuntimeError("thinning exploded")

    session.thin_previous_tool_results = _boom

    with (
        patch("robothor.engine.context_fit.estimate_tokens", return_value=10_000_000),
        patch(
            "robothor.engine.context.maybe_compress",
            new=AsyncMock(return_value=[{"role": "user", "content": "smaller"}]),
        ) as compress,
    ):
        await _run(session, _config(eager=True))

    assert compress.called, "a thinning error swallowed the compaction behind it"
