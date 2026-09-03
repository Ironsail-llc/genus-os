"""Compaction is gated on MESSAGE COUNT, so it does nothing to a huge one.

Measured 2026-08-27, directly against the real function:

    msgs   tokens                reduction
      9     90,006 ->  90,006      0.0%
     17    180,012 -> 180,012      0.0%
     21    225,015 -> 225,015      0.0%

`compact_conversation` returns unchanged whenever
`len(messages) <= KEEP_RECENT + 1` (compaction.py:559), and KEEP_RECENT is 20.
Token count is never consulted for that decision. So the failure shape is
"few, enormous messages" — which is exactly what large tool results produce,
and the documented case here is a 750KB `get_entity` result re-sent every turn.

This is not merely wasted spend. The local tier's window is now 65,536 tokens
(model_registry), and the engine sends num_ctx from that number: a 225,000-token
conversation in 21 messages is handed to a server that allocated room for a
third of it. Compaction returning "nothing to do" is the last thing between
that conversation and silent server-side truncation.

The fix is a token-bound fallback, not a smaller KEEP_RECENT: keeping twenty
recent messages is right when they are small, and the count was never the
thing that mattered.
"""

from __future__ import annotations

import asyncio

from robothor.engine.compaction import compact
from robothor.engine.context import KEEP_RECENT, estimate_tokens

_MODEL = ["openrouter/xiaomi/mimo-v2.5"]


def _fat(pairs: int, chars: int = 90_000) -> list[dict]:
    """A conversation of few, enormous messages."""
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": f"call {i}"})
        msgs.append({"role": "user", "content": "X" * chars})
    return msgs


def _compact(msgs, threshold=20_000, drain_to=10_000):
    return asyncio.run(compact(msgs, models=_MODEL, threshold=threshold, drain_to=drain_to))


class TestTheCountFloorNoLongerBlocksIt:
    def test_a_conversation_under_the_count_floor_is_still_reduced(self):
        """21 messages, 225k tokens — the exact shape that reduced 0.0%."""
        msgs = _fat(10)
        before = estimate_tokens(msgs)
        assert len(msgs) <= KEEP_RECENT + 1, "fixture must sit under the count floor"
        assert before > 100_000

        result = _compact(msgs)
        assert result.tokens_after < before * 0.7, (
            f"compaction reclaimed nothing: {before} -> {result.tokens_after}"
        )

    def test_a_tiny_conversation_is_left_alone(self):
        """The floor exists for a reason: don't shred a short exchange."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = _compact(msgs, threshold=20_000)
        assert result.tokens_after == result.tokens_before
        assert result.messages == msgs

    def test_a_conversation_under_the_threshold_is_untouched(self):
        """Token budget still decides WHETHER to compact at all."""
        msgs = _fat(2, chars=100)
        result = _compact(msgs, threshold=1_000_000)
        assert result.passes_used == 0
        assert result.tokens_after == result.tokens_before


class TestItReportsWhatItActuallyDid:
    def test_an_ineffective_pass_is_not_reported_as_a_reduction(self):
        """A pass that reclaims nothing must not read as success. The runner
        logs 'Proactive compaction at iter N' on the strength of this result."""
        msgs = _fat(10)
        result = _compact(msgs)
        if result.tokens_after >= result.tokens_before:
            assert result.passes_used == 0, (
                "reported passes without reclaiming anything — that is what made "
                "the inert case look like it was working"
            )

    def test_the_result_carries_honest_before_and_after(self):
        msgs = _fat(10)
        result = _compact(msgs)
        assert result.tokens_before == estimate_tokens(msgs)
        assert result.tokens_after == estimate_tokens(result.messages)


class TestTheCallerDoesNotReImplementTheFloor:
    """The fix to `compact` was unreachable for an hour.

    `maybe_compress` (context.py:176) carried its OWN copy of the same
    `len(messages) <= KEEP_RECENT + 1` gate and returned before `compact` was
    ever called. Two copies of one rule: fixing the function changed nothing
    through the path the runner actually uses, and only re-running the probe
    against `maybe_compress` — not the unit test on `compact` — showed it.
    """

    def test_the_real_caller_reduces_a_fat_conversation(self):
        from robothor.engine.context import maybe_compress

        msgs = _fat(10)
        before = estimate_tokens(msgs)
        out = asyncio.run(maybe_compress(list(msgs), _MODEL, threshold=20_000))
        assert estimate_tokens(out) < before * 0.7, (
            "maybe_compress still short-circuits before compact() can act"
        )

    def test_the_caller_leaves_a_short_exchange_alone(self):
        from robothor.engine.context import maybe_compress

        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        assert asyncio.run(maybe_compress(list(msgs), _MODEL, threshold=1)) == msgs

    def test_the_count_floor_lives_in_exactly_one_place(self):
        """Source-anchored, so the duplicate cannot quietly return."""
        from pathlib import Path

        ctx = (Path(__file__).resolve().parents[1] / "context.py").read_text()
        code = "\n".join(
            ln for ln in ctx.splitlines() if not ln.lstrip().startswith(("#", '"', "'"))
        )
        assert "KEEP_RECENT + 1" not in code, (
            "context.py re-implements compaction's count floor; the decision "
            "belongs to compact() alone"
        )


class TestNoCallerRelinesOnTheDefaultThreshold:
    """COMPRESS_THRESHOLD is 80,000. The local tier's window is 65,536.

    Every production caller passes an explicit, model-aware threshold today
    (llm_client uses proactive_compaction_threshold, which yields 32,768 for
    the local tier), so there is no live exposure. But a new caller that omits
    it would compact at 80,000 for a model that can only hold 65,536 — i.e.
    never in time, and the engine now sends num_ctx from that same registry
    number, so the server would truncate instead.
    """

    def test_no_production_caller_omits_the_threshold(self):
        import ast
        from pathlib import Path

        engine = Path(__file__).resolve().parents[1]
        offenders = []
        for py in engine.rglob("*.py"):
            if "/tests/" in str(py) or py.name == "context.py":
                continue
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            offenders.extend(
                f"{py.name}:{n.lineno}"
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", "")) == "maybe_compress"
                and not any(k.arg == "threshold" for k in n.keywords)
            )
        assert not offenders, (
            "maybe_compress called without an explicit threshold at "
            f"{offenders} — the 80,000 default is larger than the local tier's "
            "65,536-token window"
        )

    def test_the_local_tier_threshold_leaves_room_for_output(self):
        from robothor.engine.model_registry import get_model_limits, get_output_tokens
        from robothor.engine.run_budget import proactive_compaction_threshold

        window = get_model_limits("ollama_chat/qwen3.8:27b").max_input_tokens
        threshold = proactive_compaction_threshold(window)
        assert threshold + get_output_tokens("ollama_chat/qwen3.8:27b", threshold) <= window
