"""Extraction for a benchmark history: prefetched concurrently, cached on disk.

The write path stays sequential (supersession depends on order); only the
model calls it would make run ahead, and a later ingestion of the same
history (another code version) pays for none it has already made.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import facts
from robothor.memory.tests.test_eval_bench import _history


class _FakeExtract:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.live = 0
        self.peak = 0

    async def __call__(self, content, max_retries=3, *, timeout=180.0, context=None):
        self.calls.append(facts.build_extraction_prompt(content, context=context))
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.01)
        self.live -= 1
        return facts.Extraction([{"fact_text": f"Fact from {content[:20]}", "category": "event"}])


@pytest.mark.asyncio
async def test_prefetch_runs_every_extraction_once_concurrently(tmp_path, monkeypatch):
    fake = _FakeExtract()
    monkeypatch.setattr(facts, "extract_facts_detailed", fake)
    store = eb.ExtractionStore(tmp_path, model="m")
    stats = await eb.prefetch_extractions(_history(), store, concurrency=4)
    # Two sessions, each one window, user and assistant role: four calls.
    assert stats == {"inputs": 4, "cached": 0, "computed": 4, "failed": 0}
    assert fake.peak > 1
    again = await eb.prefetch_extractions(_history(), store, concurrency=4)
    assert again["cached"] == 4 and len(fake.calls) == 4


@pytest.mark.asyncio
async def test_the_write_path_reads_the_cache_at_the_pinned_date(tmp_path, monkeypatch):
    fake = _FakeExtract()
    monkeypatch.setattr(facts, "extract_facts_detailed", fake)
    store = eb.ExtractionStore(tmp_path, model="m")
    await eb.prefetch_extractions(_history(), store, concurrency=2)
    session = _history().sessions[0]
    from robothor.memory.conversation_ingest import role_transcripts

    turns = role_transcripts(eb.transcript_windows(session.turns)[0])
    with eb.cached_extraction(store), facts.extraction_reference_date("2023-05-20"):
        out = await facts.extract_facts_detailed(turns["user"], context=turns["assistant"])
    assert out.facts and len(fake.calls) == 4, "served from the cache, no new call"
    # Another date is another prompt: not served from the cache.
    with eb.cached_extraction(store), facts.extraction_reference_date("1999-01-01"):
        await facts.extract_facts_detailed(turns["user"], context=turns["assistant"])
    assert len(fake.calls) == 5
    assert facts.extract_facts_detailed is fake, "the original is restored on exit"


@pytest.mark.asyncio
async def test_the_cache_survives_a_restart_and_is_keyed_by_model(tmp_path, monkeypatch):
    fake = _FakeExtract()
    monkeypatch.setattr(facts, "extract_facts_detailed", fake)
    await eb.prefetch_extractions(
        _history(), eb.ExtractionStore(tmp_path, model="m"), concurrency=2
    )
    reloaded = await eb.prefetch_extractions(
        _history(), eb.ExtractionStore(tmp_path, model="m"), concurrency=2
    )
    assert reloaded["cached"] == 4
    other = await eb.prefetch_extractions(
        _history(), eb.ExtractionStore(tmp_path, model="other"), concurrency=2
    )
    assert other["computed"] == 4


@pytest.mark.asyncio
async def test_a_failed_extraction_is_not_cached(tmp_path, monkeypatch):
    async def failing(content, max_retries=3, *, timeout=180.0, context=None):
        return facts.Extraction([], "timeout")

    monkeypatch.setattr(facts, "extract_facts_detailed", failing)
    store = eb.ExtractionStore(tmp_path, model="m")
    stats = await eb.prefetch_extractions(_history(), store, concurrency=2)
    assert stats["failed"] == 4 and stats["computed"] == 0
    assert (
        not (tmp_path / "extractions.jsonl").exists()
        or not (tmp_path / "extractions.jsonl").read_text(encoding="utf-8").strip()
    )


@pytest.mark.asyncio
async def test_prefetch_defaults_to_one_call_in_flight(tmp_path):
    """A local model server that runs one request at a time queues the rest,
    and a queued extraction can outlive the product's extraction timeout; so
    one call in flight unless the operator says the server runs more."""
    from robothor.memory import eval_systems as es
    from robothor.memory.tests.test_eval_bench import _bench, _EchoSystem, _FakeChat

    seen: list[int] = []

    async def ingest(history, tenant, **kwargs):
        seen.append(kwargs.get("prefetch_concurrency"))
        return eb.IngestResult(eb.SeededWorld(tenant_ids=(tenant,)), {}, 0, 0, 0.0)

    es.register_system("echo_bench", _EchoSystem)
    try:
        common = {
            "systems": ["echo_bench"],
            "ingest_fn": ingest,
            "cleanup_fn": lambda _t: {},
            "chat": _FakeChat(),
            "validate_judge": False,
            "ingest_mode": "extract",
        }
        await eb.run_benchmark(_bench(1), **common)
        await eb.run_benchmark(_bench(1), extract_concurrency=3, **common)
    finally:
        es.unregister_system("echo_bench")
    assert seen == [1, 3]
