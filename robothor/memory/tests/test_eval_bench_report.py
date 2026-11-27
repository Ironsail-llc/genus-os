"""The benchmark report and CLI: the segments caveat, evidence recall, budgets."""

from __future__ import annotations

import json

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory.tests.test_eval_bench_data import _lme_entry


def test_markdown_states_the_segments_caveat_and_evidence_recall():
    recs = [
        bd.BenchRecord(
            "recall", "a", "multi-session", False, 1.0, 10, 1.0, 1.0, evidence_recall=1.0
        ),
        bd.BenchRecord(
            "recall", "b", "multi-session", False, 0.0, 10, 1.0, 1.0, evidence_recall=0.0
        ),
    ]
    report = {
        "params": {
            "benchmark": "longmemeval",
            "variant": "s",
            "ingest_mode": "segments",
            "answer_model": "m",
            "judge_model": "j",
            "token_budget": 1000,
            "questions": 2,
        },
        "systems": bd.summarise_bench(recs, benchmark="longmemeval"),
        "by_split": {},
    }
    md = eb.format_bench_markdown(report)
    assert "extracted facts are absent" in md
    assert "Session evidence" in md and "50.0%" in md
    assert "missing evidence 1" in md


def test_cli_dry_run_takes_several_budgets(tmp_path, capsys):
    from robothor.cli import _build_parser
    from robothor.cli.memory_eval import cmd_memory_eval

    data = [_lme_entry(f"q{i}", "multi-session", "Q?", "A") for i in range(2)]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    args = _build_parser().parse_args(
        [
            "memory-eval",
            "--benchmark",
            "longmemeval",
            "--benchmark-path",
            str(path),
            "--systems",
            "recall",
            "--answer-models",
            "openai/gpt-4o",
            "--judge-model",
            "openai/gpt-4o-2024-08-06",
            "--context-tokens",
            "1000,4000",
            "--dry-run",
        ]
    )
    assert cmd_memory_eval(args) == 0
    out = capsys.readouterr().out
    assert "context budget 1000 tokens" in out and "context budget 4000 tokens" in out


def test_cli_cells_pick_budget_and_model_pairs(tmp_path, capsys):
    from robothor.cli import _build_parser
    from robothor.cli.memory_eval import cmd_memory_eval

    data = [_lme_entry(f"q{i}", "multi-session", "Q?", "A") for i in range(2)]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    args = _build_parser().parse_args(
        [
            "memory-eval",
            "--benchmark",
            "longmemeval",
            "--benchmark-path",
            str(path),
            "--systems",
            "recall",
            "--judge-model",
            "openai/gpt-4o-2024-08-06",
            "--dry-run",
            "--cells",
            "1000:openai/gpt-5-mini,1000:openai/gpt-4o,4000:openai/gpt-5-mini",
        ]
    )
    assert cmd_memory_eval(args) == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.startswith("| recall |")]
    assert len(rows) == 3
    assert "Iteration total" in out
