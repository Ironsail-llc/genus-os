"""The comparison this project has been making was against the wrong harness.

`baselines.json` holds OpenClaw's per-task scores, and every standing report
has read "Genus vs OpenClaw". WildClawBench also publishes the same 60 tasks
under Claude Code, Codex CLI and Hermes Agent, and those numbers say OpenClaw
is the best harness on **zero** of four models while Hermes is best on three.

So "front-runner" is a higher bar than the one that has been measured. On
MiMo V2 Pro — the model `bench/wildclaw/agent.yaml` pins — OpenClaw scores
40.2 and Hermes 48.1. Beating the number this project has been tracking
would still leave it 8 points behind the actual leader.
"""

from __future__ import annotations

from bench.wildclaw.harness_baselines import (
    best_harness,
    harness_scores_for,
    leader_gap,
)


class TestTheDataIsReadable:
    def test_every_published_model_is_present(self):
        for model in ("GPT-5.4", "GLM 5", "MiMo V2 Pro", "MiniMax M2.7"):
            assert harness_scores_for(model), f"missing {model}"

    def test_an_unknown_model_is_empty_not_an_error(self):
        assert harness_scores_for("no-such-model") == {}


class TestWhoActuallyLeads:
    def test_hermes_leads_three_of_four_models(self):
        leaders = [best_harness(m) for m in ("GPT-5.4", "GLM 5", "MiMo V2 Pro", "MiniMax M2.7")]
        assert leaders.count("Hermes Agent") == 3

    def test_openclaw_leads_none(self):
        leaders = [best_harness(m) for m in ("GPT-5.4", "GLM 5", "MiMo V2 Pro", "MiniMax M2.7")]
        assert "OpenClaw" not in leaders, (
            "the harness this project benchmarks against is not the leader on any model"
        )

    def test_the_gap_to_the_leader_is_reported_not_the_gap_to_openclaw(self):
        """On the model our own bench pins, the leader is 7.9 points above OpenClaw."""
        gap = leader_gap("MiMo V2 Pro", 40.2)
        assert gap["leader"] == "Hermes Agent"
        assert round(gap["behind_leader"], 1) == 7.9
