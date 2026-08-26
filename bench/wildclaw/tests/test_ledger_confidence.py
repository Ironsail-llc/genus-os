"""The ledger says what it knows, and how firmly.

Measured 2026-08-26: the nightly rotation scored Productivity Flow at 28.3%
unattended, one day after a hand-driven run of the same category, same
model, same graders scored 37.6%. Nine points. The gap the whole OpenClaw
comparison turns on is 1.3.

So a single run cannot answer "are we ahead". Reading one ledger line and
concluding anything is the same error as trusting a green test over a live
probe — a number that looks like an answer and is not.

This reads every line the rotation has written and reports, per category,
the mean across runs, the observed spread, and — the part that matters —
whether the difference from the published baseline is larger than that
spread. When it is not, the honest verdict is "too close to call", and
saying so is the point.

No statistics beyond range and mean: with three or four samples a
confidence interval implies precision the data does not have.
"""

from __future__ import annotations

import json

from bench.wildclaw.ledger import CategoryStanding, read_ledger, summarize


def _line(category, mean, baseline=0.5, when="2026-08-26T04:42:00+00:00"):
    return json.dumps(
        {
            "when": when,
            "category": category,
            "mean": mean,
            "baseline_mean": baseline,
            "delta": round(mean - baseline, 4),
            "tasks_attempted": 10,
            "tasks_graded": 10,
            "harness_kills": 0,
            "per_task": {},
        }
    )


def _write(tmp_path, lines):
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestReading:
    def test_it_reads_every_line(self, tmp_path):
        p = _write(tmp_path, [_line("A", 0.4), _line("A", 0.5), _line("B", 0.6)])
        assert len(read_ledger(p)) == 3

    def test_a_malformed_line_is_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "ledger.jsonl"
        p.write_text(_line("A", 0.4) + "\nnot json\n" + _line("A", 0.5) + "\n", encoding="utf-8")
        assert len(read_ledger(p)) == 2

    def test_a_missing_ledger_is_empty_not_an_error(self, tmp_path):
        assert read_ledger(tmp_path / "nope.jsonl") == []


class TestSummarizing:
    def test_it_groups_by_category(self, tmp_path):
        rows = read_ledger(_write(tmp_path, [_line("A", 0.4), _line("A", 0.6), _line("B", 0.5)]))
        out = {s.category: s for s in summarize(rows)}
        assert out["A"].runs == 2 and out["B"].runs == 1

    def test_the_mean_is_across_runs(self, tmp_path):
        rows = read_ledger(_write(tmp_path, [_line("A", 0.4), _line("A", 0.6)]))
        assert summarize(rows)[0].mean == 0.5

    def test_the_spread_is_reported(self, tmp_path):
        rows = read_ledger(_write(tmp_path, [_line("A", 0.283), _line("A", 0.376)]))
        s = summarize(rows)[0]
        assert round(s.spread, 3) == 0.093, "the real 9-point Productivity swing"


class TestTheVerdictRespectsTheSpread:
    def test_a_gap_smaller_than_the_spread_is_too_close_to_call(self, tmp_path):
        """The actual situation: 1.3 points of gap, 9 points of spread."""
        rows = read_ledger(
            _write(tmp_path, [_line("A", 0.283, baseline=0.388), _line("A", 0.376, baseline=0.388)])
        )
        assert summarize(rows)[0].verdict == "too close to call"

    def test_a_gap_larger_than_the_spread_is_called(self, tmp_path):
        """Safety Alignment: +20.7 points, replicated. That is a real lead."""
        rows = read_ledger(
            _write(tmp_path, [_line("S", 0.677, baseline=0.47), _line("S", 0.672, baseline=0.47)])
        )
        assert summarize(rows)[0].verdict == "ahead"

    def test_a_large_deficit_is_called_too(self, tmp_path):
        rows = read_ledger(
            _write(tmp_path, [_line("C", 0.30, baseline=0.643), _line("C", 0.31, baseline=0.643)])
        )
        assert summarize(rows)[0].verdict == "behind"

    def test_one_run_is_never_conclusive(self, tmp_path):
        """With a single sample there is no observed spread at all, and
        pretending otherwise is exactly the error this exists to stop."""
        rows = read_ledger(_write(tmp_path, [_line("A", 0.9, baseline=0.1)]))
        s = summarize(rows)[0]
        assert s.verdict == "one run — not yet conclusive"

    def test_a_category_with_no_baseline_says_so(self, tmp_path):
        p = tmp_path / "l.jsonl"
        p.write_text(
            json.dumps({"category": "X", "mean": 0.4, "baseline_mean": None}) + "\n",
            encoding="utf-8",
        )
        assert summarize(read_ledger(p))[0].verdict == "no baseline"


class TestRendering:
    def test_the_report_names_every_category(self, tmp_path):
        from bench.wildclaw.ledger import render

        rows = read_ledger(_write(tmp_path, [_line("01_Productivity_Flow", 0.283, 0.388)]))
        text = render(summarize(rows))
        assert "01_Productivity_Flow" in text

    def test_it_shows_how_many_runs_back_each_number_is(self, tmp_path):
        from bench.wildclaw.ledger import render

        rows = read_ledger(_write(tmp_path, [_line("A", 0.4), _line("A", 0.6)]))
        assert "2 runs" in render(summarize(rows))

    def test_an_empty_ledger_renders_a_sentence_not_a_crash(self, tmp_path):
        from bench.wildclaw.ledger import render

        assert render([]).strip() != ""


class TestStandingIsSortedByConfidence:
    def test_called_verdicts_come_before_uncertain_ones(self, tmp_path):
        rows = read_ledger(
            _write(
                tmp_path,
                [
                    _line("uncertain", 0.50, 0.51),
                    _line("uncertain", 0.42, 0.51),
                    _line("clear", 0.68, 0.47),
                    _line("clear", 0.67, 0.47),
                ],
            )
        )
        assert [s.category for s in summarize(rows)][0] == "clear"


def test_a_standing_is_comparable_for_stable_output():
    a = CategoryStanding("A", 2, 0.5, 0.1, 0.4, "ahead")
    assert a.category == "A" and a.runs == 2
