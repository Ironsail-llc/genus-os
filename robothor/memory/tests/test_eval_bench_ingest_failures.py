"""Ingest failures are loud, retried, and never scored.

An embedder that hands back a zero-length vector must not reach the write
path; a history whose ingest still fails is excluded and counted, and no
model is asked a question about memory that was never written.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _bench, _EchoSystem, _fake_ingest, _FakeChat


@pytest.fixture
def echo():
    es.register_system("echo_bench", _EchoSystem)
    yield "echo_bench"
    es.unregister_system("echo_bench")


@pytest.mark.asyncio
async def test_a_bad_vector_is_retried_then_refused(monkeypatch):
    monkeypatch.setattr(eb, "_embed_backoff", lambda _attempt: 0.0)
    calls = 0

    async def flaky(texts):
        nonlocal calls
        calls += 1
        return [[] if calls == 1 else [0.1] * 4 for _ in texts]

    embed = eb.validated_embedder(flaky, dim=4)
    assert await embed(["a", "b"]) == [[0.1] * 4, [0.1] * 4]
    assert calls == 2

    async def broken(texts):
        return [[] for _ in texts]

    with pytest.raises(RuntimeError, match="0 dimensions"):
        await eb.validated_embedder(broken, dim=4)(["a"])


@pytest.mark.asyncio
async def test_a_failed_ingest_is_retried(echo):
    attempts = 0
    ok = _fake_ingest([])

    async def flaky(history, tenant, **k):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("different vector dimensions 1024 and 0")
        return await ok(history, tenant, **k)

    report = await eb.run_benchmark(
        _bench(1),
        systems=[echo],
        ingest_fn=flaky,
        cleanup_fn=lambda _t: {},
        chat=_FakeChat(),
        validate_judge=False,
    )
    assert attempts == 2
    assert report["systems"][echo]["overall"]["n"] == 1
    assert report["excluded"] == []


@pytest.mark.asyncio
async def test_a_history_that_never_ingests_is_excluded_not_scored(echo):
    chat = _FakeChat()
    ok = _fake_ingest([])

    async def one_bad(history, tenant, **k):
        if history.id == "h0":
            raise RuntimeError("different vector dimensions 1024 and 0")
        return await ok(history, tenant, **k)

    report = await eb.run_benchmark(
        _bench(2),
        systems=[echo],
        ingest_fn=one_bad,
        cleanup_fn=lambda _t: {},
        chat=chat,
        validate_judge=False,
    )
    assert report["systems"][echo]["overall"]["n"] == 1
    assert report["systems"][echo]["overall"]["errors"] == 0
    (ex,) = report["excluded"]
    assert ex["question_id"] == "q0" and "vector dimensions" in ex["reason"]
    assert report["params"]["excluded_questions"] == 1
    assert all("q0" not in str(r["question_id"]) for r in report["records"])
    # Only q1 was asked and judged.
    assert len(chat.calls) == 2
    assert "Excluded" in eb.format_bench_markdown(report)


@pytest.mark.asyncio
async def test_prefill_retries_and_counts_failed_histories(echo, tmp_path):
    attempts: dict[str, int] = {}
    ok = _fake_ingest([])

    async def flaky(history, tenant, **k: Any):
        attempts[history.id] = attempts.get(history.id, 0) + 1
        if history.id == "h0" or attempts[history.id] < 2:
            raise RuntimeError("different vector dimensions 1024 and 0")
        return await ok(history, tenant, **k)

    bench = _bench(2)
    stats = await eb.prefill_contexts(
        bench,
        bench.questions,
        systems=[echo],
        token_budgets=[1000],
        cache_dir=str(tmp_path),
        ingest_fn=flaky,
        cleanup_fn=lambda _t: {},
    )
    assert attempts["h1"] == 2 and attempts["h0"] == eb.INGEST_ATTEMPTS
    assert stats["ingested"] == 1 and stats["failed_histories"] == 1
