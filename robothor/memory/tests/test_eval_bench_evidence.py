"""Evidence recall, one ingestion for several budgets, concurrent answering.

Each loss must be attributable to retrieval or to the answer model, so every
record says what share of the question's evidence sessions reached the
context that was actually sent.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _bench, _fake_ingest, _FakeChat
from robothor.memory.tests.test_eval_bench_data import _lme_entry


def test_longmemeval_questions_carry_their_evidence_sessions(tmp_path):
    entry = _lme_entry("q1", "multi-session", "Q?", "A")
    entry["answer_session_ids"] = ["answer_1", "s-later"]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    (q,) = bd.load_benchmark("longmemeval", path).questions
    assert q.evidence_sessions == ("answer_1", "s-later")


def test_evidence_recall_counts_sessions_in_the_sent_context():
    q = bd.BenchQuestion("q", "h", "multi-session", "Q?", "A", evidence_sessions=("s1", "s2"))
    items = [{"id": "s1#0u"}, {"id": "s1#1a"}, {"id": "s9#0u"}, {"id": "fact:3"}]
    assert eb.evidence_recall(q, items) == pytest.approx(0.5)
    assert eb.evidence_recall(q, []) == 0.0
    abst = bd.BenchQuestion(
        "a_abs", "h", "multi-session", "Q?", "A", abstention=True, evidence_sessions=("s1",)
    )
    assert eb.evidence_recall(abst, items) is None
    assert eb.evidence_recall(bd.BenchQuestion("n", "h", "t", "Q", "A"), items) is None


def test_summary_reports_evidence_recall_per_type():
    recs = [
        bd.BenchRecord("r", "a", "multi-session", False, 1.0, 10, 1.0, 1.0, evidence_recall=1.0),
        bd.BenchRecord("r", "b", "multi-session", False, 0.0, 10, 1.0, 1.0, evidence_recall=0.0),
        bd.BenchRecord(
            "r", "c_abs", "multi-session", True, 1.0, 10, 1.0, 1.0, evidence_recall=None
        ),
    ]
    m = bd.summarise_bench(recs, benchmark="longmemeval")["r"]
    assert m["by_type"]["multi-session"]["evidence_recall"] == pytest.approx(0.5)
    assert m["by_type"]["multi-session"]["evidence_n"] == 2
    # Losses split by cause: wrong with all evidence present is the answer model's.
    assert m["overall"]["wrong_with_evidence"] == 0
    assert m["overall"]["wrong_missing_evidence"] == 1


class _SessionEcho:
    name = "echo_sessions"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        return es.ContextBundle(
            system=self.name,
            items=es.fill_budget(
                [("answer_1#0u", "turn", "User: " + "x" * (4 * token_budget - 6))], token_budget
            ),
            budget=token_budget,
            equal_budget=True,
            abstained=False,
            retrieval_ms=1.0,
        )


@pytest.fixture
def session_system():
    es.register_system("echo_sessions", _SessionEcho)
    yield "echo_sessions"
    es.unregister_system("echo_sessions")


def _bench_with_evidence(n: int) -> bd.Benchmark:
    b = _bench(n)
    qs = tuple(
        bd.BenchQuestion(
            q.id,
            q.history_id,
            q.question_type,
            q.question,
            q.answer,
            q.as_of,
            evidence_sessions=("answer_1",),
        )
        for q in b.questions
    )
    return bd.Benchmark(b.name, b.variant, b.histories, qs, source=b.source)


@pytest.mark.asyncio
async def test_records_carry_evidence_recall(session_system):
    report = await eb.run_benchmark(
        _bench_with_evidence(1),
        systems=[session_system],
        ingest_fn=_fake_ingest([]),
        cleanup_fn=lambda _t: {},
        chat=_FakeChat(),
        validate_judge=False,
    )
    assert report["records"][0]["evidence_recall"] == 1.0


@pytest.mark.asyncio
async def test_one_ingestion_serves_every_budget(session_system, tmp_path):
    log: list[str] = []
    cache = str(tmp_path / "cache")
    bench = _bench_with_evidence(2)
    stats = await eb.prefill_contexts(
        bench,
        bench.questions,
        systems=[session_system],
        token_budgets=[1000, 4000],
        cache_dir=cache,
        ingest_fn=_fake_ingest(log),
        cleanup_fn=lambda _t: {},
    )
    assert len(log) == 2 and stats["contexts"] == 4
    tokens: dict[int, float] = {}
    for budget in (1000, 4000):
        report = await eb.run_benchmark(
            bench,
            systems=[session_system],
            token_budget=budget,
            cache_dir=cache,
            ingest_fn=_fake_ingest(log),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat(),
            validate_judge=False,
        )
        assert report["systems"][session_system]["overall"]["n"] == 2
        tokens[budget] = report["systems"][session_system]["overall"]["mean_tokens"]
    assert len(log) == 2, "no history is ingested again"
    # Each budget really was retrieved at its own size.
    assert tokens[4000] > 1000 >= tokens[1000]


@pytest.mark.asyncio
async def test_answers_run_concurrently_across_histories(session_system, tmp_path):
    live = peak = 0

    class _Slow(_FakeChat):
        async def __call__(self, messages, **k):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return await super().__call__(messages, **k)

    await eb.run_benchmark(
        _bench_with_evidence(6),
        systems=[session_system],
        ingest_fn=_fake_ingest([]),
        cleanup_fn=lambda _t: {},
        chat=_Slow(),
        validate_judge=False,
        concurrency=4,
    )
    assert 1 < peak <= 4
