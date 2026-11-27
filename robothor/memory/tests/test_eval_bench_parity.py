"""Product-parity mode: the benchmark asks memory the way an agent does.

``search_memory_tool`` calls the ``search_memory`` tool handler with only a
query (the tool's default limit, context cap and legs), as the conversation's
owner, after the product's retention has run as of the question's date, and
hands the answer model the tool message the agent would receive. Every
report says per cell whether it is product-parity or harness-only and lists
what a harness-only cell does differently.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _bench, _fake_ingest, _FakeChat

_TOOL_RESULT = {
    "query_class": "factual",
    "results": [
        {
            "id": 7,
            "fact": 'User: I graduated in Business Administration. "Quoted"',
            "kind": "segment",
            "start_ref": 101,
            "source_ref": "bench:h0:s0",
        },
        {"id": 9, "fact": "Alice studied business.", "source": "fact"},
    ],
}


@pytest.fixture
def tool(monkeypatch):
    from robothor.engine.tools.handlers import memory as tool_memory

    calls: list[dict] = []

    async def _handler(args, ctx, *, record_access=True):
        calls.append({"args": args, "ctx": ctx, "record_access": record_access})
        return json.loads(json.dumps(_TOOL_RESULT))

    monkeypatch.setitem(tool_memory.HANDLERS, "search_memory", _handler)
    retention: list[tuple[int, str]] = []

    def _cleanup(days, tenant_id=None):
        retention.append((days, tenant_id))
        return 0

    monkeypatch.setattr("robothor.memory.segments.cleanup_stale_segments", _cleanup)
    monkeypatch.setattr(es, "_utc_now", lambda: datetime(2023, 9, 1, 10, 0, tzinfo=UTC))
    return calls, retention


def _handle(reader="p-1"):
    seeded = eb.SeededWorld(tenant_ids=(eb.BENCH_TENANT,))
    seeded.reader_person_id = reader
    seeded.turn_message_ids["s0#0u"] = 101

    async def _embed(_t):
        return [0.0]

    return eb.BenchHandle(None, seeded, eb.BENCH_TENANT, f"{eb.BENCH_TENANT}-shadow", _embed)


@pytest.mark.asyncio
async def test_the_tool_is_called_as_an_agent_calls_it(tool):
    calls, retention = tool
    (q,) = _bench(1).questions  # asked on 2023/06/02
    bundle = await es.make_systems(["search_memory_tool"])[0].retrieve(
        q, world_handle=_handle(), token_budget=1000
    )
    (call,) = calls
    assert call["args"] == {"query": q.question}, "no limit: the tool's default"
    assert call["record_access"] is False
    ctx = call["ctx"]
    assert ctx.tenant_id == eb.BENCH_TENANT
    assert ctx.private_reader.person_id == "p-1", "the reader owns the conversation"
    # Product retention (90 days) measured from the question's date, by the
    # product's own nightly cleanup: 91 days from 2023-06-02 to 2023-09-01.
    from robothor.memory.segments import SEGMENT_RETENTION_DAYS

    assert retention == [(SEGMENT_RETENTION_DAYS + 91, eb.BENCH_TENANT)]
    assert bundle.equal_budget is False and bundle.budget is None
    assert [i.id for i in bundle.items] == ["s0#0u", "fact:9"]


@pytest.mark.asyncio
async def test_the_answer_model_reads_exactly_the_tool_message(tool):
    report = await eb.run_benchmark(
        _bench(1),
        systems=["search_memory_tool"],
        ingest_fn=_fake_ingest([]),
        cleanup_fn=lambda _t: {},
        chat=(chat := _FakeChat()),
        validate_judge=False,
    )
    from robothor.engine.session import AgentSession

    session = AgentSession("memory-eval", tenant_id=eb.BENCH_TENANT)
    session.record_tool_call("search_memory", {"query": "x"}, _TOOL_RESULT, tool_call_id="c")
    delivered = session.messages[-1]["content"]
    assert delivered.startswith('<untrusted_content source="search_memory">')
    answer_prompt = chat.calls[0]
    assert delivered in answer_prompt
    assert "### Session" not in answer_prompt
    assert report["records"][0]["tokens"] == es.estimate_tokens(delivered)


def test_every_cell_says_whether_it_is_product_parity():
    parity = eb.cell_mode("search_memory_tool", 1000, "segments")
    assert parity["mode"] == "product-parity"
    harness = eb.cell_mode("recall", 4000, "segments")
    assert harness["mode"] == "harness-only"
    text = " ".join(harness["differences"])
    for needle in ("4000 tokens", "4000 chars", "limit 60", "fan-out", "36500", "search_facts"):
        assert needle in text, needle
    # Parity still says what it does not exercise.
    assert any("segments only" in d for d in parity["differences"])
    assert eb.cell_mode("someone_else", 1000, "segments")["mode"] == "harness-only"


@pytest.mark.asyncio
async def test_the_report_states_the_mode_of_every_cell(tool):
    report = await eb.run_benchmark(
        _bench(1),
        systems=["search_memory_tool"],
        ingest_fn=_fake_ingest([]),
        cleanup_fn=lambda _t: {},
        chat=_FakeChat(),
        validate_judge=False,
    )
    assert report["params"]["cells"]["search_memory_tool"]["mode"] == "product-parity"
    md = eb.format_bench_markdown(report)
    assert "| search_memory_tool | product-parity |" in md
    assert "Product flags" in md


class _Plain:
    name = "plain_first"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        order.append(self.name)
        return es.ContextBundle(self.name, (), token_budget, True, True, 0.0)


order: list[str] = []


@pytest.mark.asyncio
async def test_parity_retrieves_after_every_harness_cell_of_its_history(tool, monkeypatch):
    calls, _ = tool
    order.clear()
    original = es.SearchMemoryToolSystem.retrieve

    async def _spy(self, case, **k):
        order.append("search_memory_tool")
        return await original(self, case, **k)

    monkeypatch.setattr(es.SearchMemoryToolSystem, "retrieve", _spy)
    es.register_system("plain_first", _Plain)
    try:
        await eb.run_benchmark(
            _bench(1),
            systems=["search_memory_tool", "plain_first"],
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat(),
            validate_judge=False,
        )
    finally:
        es.unregister_system("plain_first")
    # Retention deletes old segments, so it must not reach the harness cells.
    assert order == ["plain_first", "search_memory_tool"]
