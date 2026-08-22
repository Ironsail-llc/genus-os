"""A tool that always dies at the cap is capped too low — say so.

`_LONG_RUNNING_TOOLS` is a hand-maintained set, and it has now been wrong three
separate times in one night:

* `benchmark_run_fleet` / `benchmark_run_for_agent` were missing while
  `benchmark_run` (which nothing schedules) was present — #330,
* `buddy_review_pass` was missing: 8 of its last 10 calls died at exactly 120s
  and it has NEVER completed above the cap, so main has had no buddy review
  since 2026-08-19 and vision-monitor since 2026-08-17,
* `deep_reason` (4 of 18) and `look` (3 of 70) die there too.

Adding three more names to the set fixes today and guarantees a fourth drift.
The durable half is a detector: when a tool's calls pile up at exactly the
configured cap, the cap is the thing failing, not the tool. That signature is
unmistakable in the data and needs no list to maintain.

This is the same shape as every other defect this campaign has surfaced — an
infrastructure limit recorded as the callee's fault — and the same fix: derive
the finding from what actually happened instead of from a list someone has to
remember to update.
"""

from __future__ import annotations

from robothor.engine.detectors import find_tools_capped_at_timeout


def _call(duration_ms: int, tool: str = "buddy_review_pass") -> dict:
    return {"tool": tool, "duration_ms": duration_ms}


class TestCapClustering:
    def test_a_tool_that_always_dies_at_the_cap_is_reported(self) -> None:
        """buddy_review_pass: 8 of 10 at 120s, never above it."""
        calls = [_call(120_001) for _ in range(8)] + [_call(46_000), _call(72_000)]
        found = find_tools_capped_at_timeout(calls, cap_seconds=120)
        assert len(found) == 1
        assert found[0]["tool"] == "buddy_review_pass"
        assert found[0]["capped_calls"] == 8

    def test_a_healthy_tool_is_not_reported(self) -> None:
        """Fast calls with headroom to spare say nothing about the cap."""
        calls = [_call(d, "search_memory") for d in (900, 1200, 800, 1500, 700)]
        assert find_tools_capped_at_timeout(calls, cap_seconds=120) == []

    def test_one_unlucky_timeout_is_not_a_pattern(self) -> None:
        """A single call at the cap is a blip, not a mis-set budget."""
        calls = [_call(1000, "look") for _ in range(30)] + [_call(120_000, "look")]
        assert find_tools_capped_at_timeout(calls, cap_seconds=120) == []

    def test_a_tool_that_exceeds_the_cap_is_not_capped_by_it(self) -> None:
        """If calls run well past the cap, that cap is not what is killing them."""
        calls = [_call(120_000) for _ in range(5)] + [_call(300_000) for _ in range(5)]
        assert find_tools_capped_at_timeout(calls, cap_seconds=120) == []

    def test_the_window_is_tolerant_of_jitter(self) -> None:
        """119.96s and 120.4s are the same event; exact equality would miss it."""
        calls = [_call(d) for d in (119_600, 120_100, 119_950, 120_400)] + [_call(5_000)]
        assert len(find_tools_capped_at_timeout(calls, cap_seconds=120)) == 1

    def test_a_different_cap_moves_the_window(self) -> None:
        calls = [_call(600_050) for _ in range(6)] + [_call(1_000)]
        assert find_tools_capped_at_timeout(calls, cap_seconds=120) == []
        assert len(find_tools_capped_at_timeout(calls, cap_seconds=600)) == 1

    def test_too_few_calls_to_judge(self) -> None:
        """Two calls is not evidence of a pattern either way."""
        assert find_tools_capped_at_timeout([_call(120_000), _call(120_000)], cap_seconds=120) == []

    def test_empty_input_is_silent(self) -> None:
        assert find_tools_capped_at_timeout([], cap_seconds=120) == []


class TestTheListItself:
    def test_every_tool_measured_as_capped_tonight_is_now_covered(self) -> None:
        """The three found in production on 2026-08-22."""
        from robothor.engine.runner import _LONG_RUNNING_TOOLS

        for tool in ("buddy_review_pass", "deep_reason", "look"):
            assert tool in _LONG_RUNNING_TOOLS, (
                f"{tool} died at the 120s cap in production and is still not declared long-running"
            )
