"""Re-scoring a finished run from its saved contexts: no model call, no database.

An earlier report holds only session-level evidence. Its contexts are in the
run's cache, so the answer-turn evidence and the three-way loss split can be
computed for it after the fact, and the old buckets set beside the new.
"""

from __future__ import annotations

import json

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _fake_ingest, _FakeChat
from robothor.memory.tests.test_eval_bench_data import _lme_entry


class _Cut:
    """The evidence session, without its answer turn."""

    name = "cut_ctx"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        return es.ContextBundle(
            system=self.name,
            items=es.fill_budget([("answer_1#0u", "turn", "User: I graduated.")], token_budget),
            budget=token_budget,
            equal_budget=True,
            abstained=False,
            retrieval_ms=1.0,
        )


@pytest.fixture
def old_run(tmp_path):
    entries = [_lme_entry(f"q{i}", "single-session-user", "What degree?", "BA") for i in range(2)]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    bench = bd.load_benchmark("longmemeval", path)
    cache = str(tmp_path / "cache")
    es.register_system("cut_ctx", _Cut)
    try:
        import asyncio

        report = asyncio.run(
            eb.run_benchmark(
                bench,
                systems=["cut_ctx"],
                cache_dir=cache,
                ingest_fn=_fake_ingest([]),
                cleanup_fn=lambda _t: {},
                chat=_FakeChat("I don't know."),
                validate_judge=False,
                answer_model="m",
                judge_model="j",
            )
        )
    finally:
        es.unregister_system("cut_ctx")
    # As an earlier version wrote it: session-level evidence only.
    for rec in report["records"]:
        rec.pop("answer_turn_recall", None)
        rec.pop("answer_turns_complete", None)
    return bench, report, cache


def test_rescore_adds_turn_evidence_from_the_saved_contexts(old_run):
    bench, report, cache = old_run

    out = eb.rescore_report(report, bench, cache_dir=cache)
    recs = out["records"]
    assert all(r["answer_turn_recall"] == 0.0 for r in recs)
    assert all(r["evidence_recall"] == 1.0 for r in recs), "session level kept beside it"
    assert out["rescore"]["linked"] == 2 and out["rescore"]["unlinked"] == 0
    # Old: wrong with every session present (blamed on the answer model).
    # New: the answer turn was never delivered.
    assert out["rescore"]["transitions"] == {
        "all_sessions_present -> answer_turns_absent": 2,
    }
    ov = out["systems"]["cut_ctx"]["overall"]
    assert ov["losses"]["answer_turns_absent"] == 2
    assert ov["losses"]["answer_turns_present_answer_wrong"] == 0
    assert out["params"]["rescored_from"]
    md = eb.format_bench_markdown(out)
    assert "Re-scored from saved contexts" in md
    assert "all_sessions_present -> answer_turns_absent" in md


def test_a_record_whose_context_is_gone_is_counted_not_guessed(old_run, tmp_path):
    bench, report, _cache = old_run
    out = eb.rescore_report(report, bench, cache_dir=str(tmp_path / "empty"))
    assert out["rescore"]["linked"] == 0 and out["rescore"]["unlinked"] == 2
    assert all(r.get("answer_turn_recall") is None for r in out["records"])


def test_a_context_is_linked_only_when_its_answer_matches(old_run):
    bench, report, cache = old_run
    report["records"][0]["answer"] = "a different answer than the cache holds"
    out = eb.rescore_report(report, bench, cache_dir=cache)
    assert out["rescore"]["linked"] == 1 and out["rescore"]["answer_mismatch"] == 1
