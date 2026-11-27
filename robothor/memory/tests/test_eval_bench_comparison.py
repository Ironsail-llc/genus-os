"""Every LongMemEval report carries a fair-comparison table.

Outside numbers come with the conditions that make them comparable or not:
source, answer model, judge, context size, whether anything was retrieved,
and the known caveats. They are collected from their sources, never
reproduced here, and the table says so. This run's own cells sit beside them
under the same columns.
"""

from __future__ import annotations

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd


def _report(benchmark="longmemeval"):
    recs = [
        bd.BenchRecord("recall", "a", "multi-session", False, 1.0, 3900, 1.0, 1.0),
        bd.BenchRecord("evidence_only", "a", "multi-session", False, 1.0, 6000, 0.0, 1.0),
    ]
    return {
        "params": {
            "benchmark": benchmark,
            "variant": "s",
            "ingest_mode": "segments",
            "answer_model": "openai/gpt-5-mini",
            "judge_model": "openai/gpt-4o-2024-08-06",
            "token_budget": 4000,
            "questions": 1,
            "cells": {
                "recall": eb.cell_mode("recall", 4000, "segments"),
                "evidence_only": eb.cell_mode("evidence_only", 4000, "segments"),
            },
        },
        "systems": bd.summarise_bench(recs, benchmark=benchmark),
        "by_split": {},
    }


def test_published_rows_name_their_conditions():
    rows = bd.PUBLISHED_LONGMEMEVAL
    assert len(rows) >= 8
    for r in rows:
        for field in (
            "system",
            "source",
            "answer_model",
            "judge",
            "context_tokens",
            "retrieval",
            "score",
            "caveats",
        ):
            assert r.get(field), (r.get("system"), field)
        assert r["source"].startswith("https://")
    full = next(r for r in rows if "full context" in r["system"].lower())
    assert full["retrieval"] == "no"


def test_the_report_marks_outside_numbers_as_collected_not_reproduced():
    md = eb.format_bench_markdown(_report())
    assert "collected, not reproduced" in md
    section = md.split("collected, not reproduced", 1)[1]
    assert (
        "| Source | System | Answer model | Judge | Context tokens | Retrieval | Score | Caveats |"
        in section
    )
    assert "arxiv.org/abs/2410.10813" in section
    # This run's cells sit under the same columns.
    assert (
        "| this run | recall (harness-only) | openai/gpt-5-mini | openai/gpt-4o-2024-08-06 | 3900 | yes |"
        in section
    )
    assert "| this run | evidence_only (harness-only) |" in section and "| 6000 | no |" in section


def test_no_comparison_table_for_a_benchmark_without_references():
    assert "collected, not reproduced" not in eb.format_bench_markdown(_report("locomo"))
