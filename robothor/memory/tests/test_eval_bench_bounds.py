"""The two bounds around retrieval, built from the benchmark itself.

``evidence_only`` hands the answer model only the question's evidence
sessions, whole, in date order: the ceiling of the answering step.
``full_history`` hands it every session: the no-retrieval baseline. Neither
ingests anything or touches a database, and the dry run prices them from
their real size.
"""

from __future__ import annotations

import json

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _FakeChat
from robothor.memory.tests.test_eval_bench_data import _lme_entry


def _bench(tmp_path) -> bd.Benchmark:
    entry = _lme_entry("q1", "single-session-user", "Which instrument did I take up?", "BA")
    entry["haystack_dates"].append("2023/05/19 (Fri) 09:00")
    entry["haystack_session_ids"].append("s-early")
    entry["haystack_sessions"].append(
        [{"role": "user", "content": "Unrelated chat about the weather in Brelford."}]
    )
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    return bd.load_benchmark("longmemeval", path)


def test_evidence_only_is_the_evidence_sessions_whole_in_date_order(tmp_path):
    bench = _bench(tmp_path)
    (q,) = bench.questions
    bundle = es.make_systems(["evidence_only"])[0].from_benchmark(bench, q)
    assert [i.id.split("#")[0] for i in bundle.items] == ["answer_1"]
    (item,) = bundle.items
    assert item.text == ("User: I took up the cello.\nAssistant: Congratulations, Alice!")
    assert item.meta["date"] == "2023/05/20 (Sat) 02:21"
    assert bundle.equal_budget is False


def test_full_history_is_every_session_in_date_order(tmp_path):
    bench = _bench(tmp_path)
    (q,) = bench.questions
    bundle = es.make_systems(["full_history"])[0].from_benchmark(bench, q)
    assert [i.id.split("#")[0] for i in bundle.items] == ["s-early", "answer_1", "s-later"]


@pytest.mark.asyncio
async def test_a_bound_runs_with_no_ingest_and_no_database(tmp_path, monkeypatch):
    def _no_db(*_a, **_k):
        raise AssertionError("a bound touches no database")

    monkeypatch.setattr("robothor.db.connection.get_connection", _no_db)

    async def _no_ingest(*_a, **_k):
        raise AssertionError("a bound ingests nothing")

    chat = _FakeChat("You took up the cello.")
    report = await eb.run_benchmark(
        _bench(tmp_path),
        systems=["evidence_only"],
        ingest_fn=_no_ingest,
        cleanup_fn=_no_db,
        chat=chat,
        validate_judge=False,
        cache_dir=str(tmp_path / "cache"),
    )
    (rec,) = report["records"]
    assert rec["evidence_recall"] == 1.0 and rec["answer_turn_recall"] == 1.0
    assert "Session Date: 2023/05/20 (Sat) 02:21" in chat.calls[0]
    assert report["params"]["cells"]["evidence_only"]["mode"] == "harness-only"
    assert not eb.needs_database(["evidence_only", "full_history"])
    assert eb.needs_database(["evidence_only", "recall"])


def test_the_dry_run_prices_a_bound_at_its_real_size(tmp_path):
    bench = _bench(tmp_path)
    est = eb.estimate_run(
        bench,
        bench.questions,
        systems=["full_history", "evidence_only"],
        answer_models=["openai/gpt-5-mini"],
        judge_model="openai/gpt-4o-2024-08-06",
        token_budget=100_000,
    )
    full, oracle = est["cells"]
    assert full["cached_contexts"] == 1 and oracle["cached_contexts"] == 1
    assert full["context_tokens"] > oracle["context_tokens"] > 0
    assert full["answer_input_tokens"] < 100_000, "the real history, not the budget"
