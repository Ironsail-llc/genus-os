"""Every published standing must name the harness actually in front.

`baselines.json` holds OpenClaw's scores and every standing this project has
produced reads "Genus vs OpenClaw". `harness_baselines.json` — added by this
repo, from WildClawBench's own published table — says OpenClaw is the best
harness on ZERO of the four models, and Hermes Agent on three. On MiMo V2 Pro,
the model `agent.yaml` pins, OpenClaw scores 40.2 and Hermes 48.1.

So matching the number this project tracks would still leave it eight points
behind the leader, and a report that prints only the OpenClaw delta lets "near
parity" stand in for "front-runner".

`best_harness` and `leader_gap` were written to say so and had only test
callers. A correction nobody can read is not a correction.
"""

from __future__ import annotations

from bench.wildclaw.harness_baselines import best_harness, leader_gap
from bench.wildclaw.ledger import CategoryStanding, render


def _standing(mean: float = 0.402) -> CategoryStanding:
    return CategoryStanding(
        category="01_Productivity_Flow",
        runs=2,
        mean=mean,
        spread=0.09,
        baseline=0.3876,
        verdict="too close to call",
    )


class TestTheLeaderIsNamed:
    def test_the_rendered_standing_names_the_front_runner(self):
        out = render([_standing()])
        assert "Hermes" in out, "the standing still reports only the OpenClaw delta"

    def test_a_partial_ledger_refuses_to_claim_a_standing(self):
        """One category graded out of six is not a harness aggregate. The first
        draft of this footer averaged 28.3 and a single-task 100.0 and printed
        "ahead of the leader" on a mean of 64.2."""
        out = render([_standing()])
        assert "No comparison yet" in out
        assert "ahead of the leader" not in out

    def test_a_complete_ledger_states_the_distance(self):
        from bench.wildclaw.ledger import BENCH_CATEGORIES

        full = [
            CategoryStanding(
                category=f"cat{i}", runs=3, mean=0.402, spread=0.02,
                baseline=0.3876, verdict="ahead",
            )
            for i in range(BENCH_CATEGORIES)
        ]
        out = render(full)
        assert "behind" in out.lower()

    def test_openclaw_is_not_presented_as_the_bar(self):
        """It leads on none of the four models we run."""
        for model in ("GLM 5", "MiMo V2 Pro", "MiniMax M2.7"):
            assert best_harness(model) == "Hermes Agent"

    def test_an_empty_ledger_still_renders(self):
        assert "ledger is empty" in render([])


class TestTheGapIsComputedFromPublishedNumbers:
    def test_matching_openclaw_still_trails_hermes_on_our_model(self):
        gap = leader_gap("MiMo V2 Pro", 40.2)
        assert gap["leader"] == "Hermes Agent"
        assert gap["behind_leader"] and gap["behind_leader"] > 7
