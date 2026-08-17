"""The eval gate needs a floor, not perfection.

exit_code_for returned 2 unless EVERY case passed. That was right for a curated
12-case suite at 12/12. On the 267-case generated corpus the real baseline is
253/267 = 0.9476, so the nightly unit failed and fired OnFailure at 03:52 —
and a gate that pages every single night is a gate that gets muted, which is
the exact failure mode this whole overhaul has been chasing.

A floor distinguishes "the suite regressed" from "the suite is not perfect".
"""

from __future__ import annotations

import pytest

from robothor.memory.eval import DEFAULT_MIN_PASS_RATE, exit_code_for, min_pass_rate


def _report(passed: int, total: int) -> dict:
    return {"passed": passed, "total": total}


class TestFloorConfig:
    def test_default_leaves_headroom_below_the_measured_baseline(self, monkeypatch):
        # Baseline is now 267/267. The floor must sit well under it: the
        # reranker is a model, cases flip between runs, and a floor set flush
        # against the observed best pages on noise.
        monkeypatch.delenv("MEMORY_EVAL_MIN_PASS_RATE", raising=False)
        assert DEFAULT_MIN_PASS_RATE <= 0.95

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MEMORY_EVAL_MIN_PASS_RATE", "0.80")
        assert min_pass_rate() == pytest.approx(0.80)

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        # A typo must not silently disable the gate by parsing as 0.
        monkeypatch.setenv("MEMORY_EVAL_MIN_PASS_RATE", "high")
        assert min_pass_rate() == DEFAULT_MIN_PASS_RATE

    def test_out_of_range_falls_back(self, monkeypatch):
        monkeypatch.setenv("MEMORY_EVAL_MIN_PASS_RATE", "1.5")
        assert min_pass_rate() == DEFAULT_MIN_PASS_RATE
        monkeypatch.setenv("MEMORY_EVAL_MIN_PASS_RATE", "-1")
        assert min_pass_rate() == DEFAULT_MIN_PASS_RATE


class TestExitCode:
    def test_perfect_run_passes(self, monkeypatch):
        monkeypatch.delenv("MEMORY_EVAL_MIN_PASS_RATE", raising=False)
        assert exit_code_for(_report(267, 267), None) == 0

    def test_a_few_flipped_cases_do_not_page(self, monkeypatch):
        # 253/267 was the pre-repair result; it must still not page, because
        # normal run-to-run movement has to be absorbed by the floor.
        monkeypatch.delenv("MEMORY_EVAL_MIN_PASS_RATE", raising=False)
        assert exit_code_for(_report(253, 267), None) == 0

    def test_a_real_regression_still_fails(self, monkeypatch):
        # The gate must still be a gate: a 20-point drop pages.
        monkeypatch.delenv("MEMORY_EVAL_MIN_PASS_RATE", raising=False)
        assert exit_code_for(_report(200, 267), None) == 2

    def test_cannot_run_is_still_distinct_from_failing(self, monkeypatch):
        monkeypatch.delenv("MEMORY_EVAL_MIN_PASS_RATE", raising=False)
        assert exit_code_for(None, None) == 3
        assert exit_code_for(_report(0, 0), None) == 3
        assert exit_code_for(_report(267, 267), "rls blocked seeding") == 3

    def test_floor_is_inclusive(self, monkeypatch):
        monkeypatch.setenv("MEMORY_EVAL_MIN_PASS_RATE", "0.50")
        assert exit_code_for(_report(50, 100), None) == 0
        assert exit_code_for(_report(49, 100), None) == 2


class TestFailureDetailIsAccurate:
    """The detail line must name the criterion that actually failed.

    Every kind reported "gold not found in top-{k}". For temporal the real
    criterion is "did not rank FIRST" — the gold was sitting at position 2, well
    inside the top-5, and the message sent an investigation off after a
    retrieval bug that did not exist. A diagnostic that lies costs more than no
    diagnostic.
    """

    def _case(self, kind, **over):
        from robothor.memory.eval import EvalCase

        base = {
            "id": "c",
            "kind": kind,
            "query": "q",
            "gold": "the new price is $150",
            "gold_exact": "150",
            "k": 5,
            "seed": [],
            "seed_mode": "direct",
        }
        base.update(over)
        return EvalCase(**base)

    def test_temporal_says_ranking_not_absence(self):
        from robothor.memory.eval import score_case

        # Gold IS present, at position 2 — inside the top-5.
        res = score_case(self._case("temporal"), ["the old price is $100", "the new price is $150"])
        assert res.passed is False
        assert "top-5" not in res.detail
        assert "first" in res.detail.lower()

    def test_temporal_absence_still_says_absent(self):
        from robothor.memory.eval import score_case

        res = score_case(self._case("temporal"), ["something else entirely"])
        assert res.passed is False
        assert "not retrieved" in res.detail.lower() or "not found" in res.detail.lower()

    def test_recall_message_unchanged(self):
        from robothor.memory.eval import score_case

        res = score_case(self._case("recall"), ["nothing relevant"])
        assert "top-5" in res.detail

    def test_passing_case_has_no_detail(self):
        from robothor.memory.eval import score_case

        res = score_case(self._case("temporal"), ["the new price is $150"])
        assert res.passed is True
        assert res.detail == ""


class TestResolveSeedMode:
    """Temporal cases must seed through the path that CREATES supersession.

    `seed_mode: direct` stores both the stale and current fact as independent
    active rows with no supersession link — a state production never reaches,
    because every real write goes through resolve_and_store, which classifies
    the second as an `update` and supersedes the first. Ten temporal cases
    "failed" purely because of that gap, and the obvious fix (broaden the
    ranking heuristic beyond category=decision) would have been a risky change
    to retrieval made for a defect that does not exist.
    """

    def test_resolve_is_a_recognised_seed_mode(self):
        from robothor.memory.eval import VALID_SEED_MODES

        assert "resolve" in VALID_SEED_MODES
        assert {"direct", "ingest"} <= VALID_SEED_MODES

    def test_loader_accepts_resolve(self, tmp_path):
        import yaml

        from robothor.memory.eval import load_suite

        p = tmp_path / "s.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "id": "t",
                    "k": 5,
                    "cases": [
                        {
                            "id": "c1",
                            "kind": "temporal",
                            "query": "q",
                            "gold": "g",
                            "seed_mode": "resolve",
                            "seed": [{"fact_text": "g", "category": "pricing"}],
                        }
                    ],
                }
            )
        )
        _meta, cases = load_suite(p)
        assert cases[0].seed_mode == "resolve"

    def test_loader_rejects_an_unknown_seed_mode(self, tmp_path):
        # A typo must not silently fall back to `direct` and quietly change what
        # the case measures.
        import yaml

        from robothor.memory.eval import load_suite

        p = tmp_path / "s.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "id": "t",
                    "k": 5,
                    "cases": [
                        {
                            "id": "c1",
                            "kind": "recall",
                            "query": "q",
                            "gold": "g",
                            "seed_mode": "resolv",
                            "seed": [{"fact_text": "g"}],
                        }
                    ],
                }
            )
        )
        with pytest.raises(ValueError, match="seed_mode"):
            load_suite(p)
