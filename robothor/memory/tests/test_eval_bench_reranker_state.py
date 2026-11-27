"""Each question records what the reranker did for its context.

A fallback or an unavailable reranker returns normal-looking rows, so a
degraded run was invisible. The state is read from the reranker's outcome
counters around each retrieval (retrievals run one at a time), kept with the
cached context, counted in the report, and a run with any degraded
retrieval says so at the top.
"""

from __future__ import annotations

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _bench, _fake_ingest, _FakeChat
from robothor.rag import reranker


def _system(outcome: str | None):
    class _S:
        name = f"rr_{outcome}"
        equal_budget = True

        async def retrieve(self, case, *, world_handle, token_budget):
            if outcome:
                reranker._STATS[outcome] += 1
            return es.ContextBundle(
                self.name,
                es.fill_budget([("s0#0u", "turn", "User: I graduated.")], token_budget),
                token_budget,
                True,
                False,
                1.0,
            )

    return _S


async def _run(outcome, tmp_path=None):
    s = _system(outcome)
    es.register_system(s.name, s)
    try:
        return await eb.run_benchmark(
            _bench(2),
            systems=[s.name],
            cache_dir=str(tmp_path) if tmp_path else None,
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat(),
            validate_judge=False,
        )
    finally:
        es.unregister_system(s.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        ("scored", "scored"),
        ("fell_back", "fell_back"),
        ("unavailable", "unavailable"),
        (None, "not_run"),
    ],
)
async def test_each_record_names_the_reranker_state(outcome, state):
    report = await _run(outcome)
    assert {r["reranker"] for r in report["records"]} == {state}
    counts = report["systems"][f"rr_{outcome}"]["overall"]["reranker"]
    assert counts == {state: 2}


@pytest.mark.asyncio
async def test_a_degraded_run_says_so_at_the_top():
    md = eb.format_bench_markdown(await _run("fell_back"))
    top = md.split("\n\n", 2)[1]
    assert "Reranker degraded" in top and "fell back for 2" in top
    clean = eb.format_bench_markdown(await _run("scored"))
    assert "Reranker degraded" not in clean
    assert "scored 2" in clean


@pytest.mark.asyncio
async def test_the_state_travels_with_the_cached_context(tmp_path):
    await _run("fell_back", tmp_path)
    again = await _run(None, tmp_path)  # served from the cache: nothing retrieved
    assert {r["reranker"] for r in again["records"]} == {"fell_back"}


def test_an_older_record_is_unrecorded():
    rec = bd.BenchRecord("r", "q", "t", False, 1.0, 1, 1.0, 1.0)
    m = bd.summarise_bench([rec], benchmark="longmemeval")["r"]["overall"]
    assert m["reranker"] == {"unrecorded": 1}
