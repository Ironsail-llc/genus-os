"""The compaction trigger must be reachable by real runs.

Measured over the 7 days ending 2026-08-24: ZERO proactive-compaction events
and ZERO compression events fleet-wide — while 9.7% of LLM calls exceeded 60K
input tokens carrying 89.6M tokens, and re-sent conversation history was 28%
of all input. The machinery (4-pass graduated compaction, RIP_18 lossless
pre-pass, drain-to-60K) was all shipped and live; the TRIGGER was not: it
fired at 0.5 x the model window, which on the fleet primary's 1M window is
524,288 tokens — 7.4x the p95 per-call input (70,598). A threshold no real
run can reach is the inert-control pattern wearing a percentage.

The trigger is now the MINIMUM of the window fraction and an absolute budget
(ROBOTHOR_COMPACTION_TRIGGER_TOKENS, default 80,000 — matching the compaction
system's own drain target's headroom), and the check runs every iteration
instead of every fifth: at ~10K tokens/iteration a 5-iteration gap overshoots
the budget by half the budget again before anyone looks.
"""

from __future__ import annotations

from robothor.engine.runner import proactive_compaction_threshold


class TestProactiveThreshold:
    def test_huge_window_is_clamped_to_the_absolute_budget(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_COMPACTION_TRIGGER_TOKENS", raising=False)
        # fleet primary: 1,048,576-token window. 0.5x = 524,288 — unreachable.
        assert proactive_compaction_threshold(1_048_576) == 80_000

    def test_small_window_still_uses_the_fraction(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_COMPACTION_TRIGGER_TOKENS", raising=False)
        # a 40K-window fallback must compact at 20K, NOT wait for 80K and
        # overflow — the min() must go both ways.
        assert proactive_compaction_threshold(40_960) == 20_480

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_COMPACTION_TRIGGER_TOKENS", "120000")
        assert proactive_compaction_threshold(1_048_576) == 120_000

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_COMPACTION_TRIGGER_TOKENS", "lots")
        assert proactive_compaction_threshold(1_048_576) == 80_000

    def test_zero_env_disables_the_clamp(self, monkeypatch):
        """0 = old behaviour (window fraction only) — the documented escape
        hatch, not a silent one."""
        monkeypatch.setenv("ROBOTHOR_COMPACTION_TRIGGER_TOKENS", "0")
        assert proactive_compaction_threshold(1_048_576) == 524_288


def test_the_loop_checks_every_iteration():
    """The %5 gate met the unreachable threshold and doubled the failure: even
    a reachable budget would be checked 10K-50K tokens late. Source-level pin
    so the gate cannot quietly return."""
    from pathlib import Path

    import robothor.engine.runner as runner_mod

    src = Path(runner_mod.__file__).read_text()
    assert "_iteration % 5 == 0" not in src, (
        "the proactive compaction check is gated to every 5th iteration again"
    )
