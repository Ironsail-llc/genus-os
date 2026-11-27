"""The benchmark's reader prompt takes its context text from the eval renderer.

``eval_answer.render_bundle`` is the one place a bundle becomes text for an
answer model, so a change to how recall reads (dates, grouping, order) reaches
the benchmark without a second formatter to keep in step. Until the shared
renderer lands the text keeps the LongMemEval session layout, with one fix:
fragments of one session are one session, not several.
"""

from __future__ import annotations

import pytest

from robothor.memory import eval_answer as ea
from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory import eval_systems as es
from robothor.memory.tests.test_eval_bench import _bench, _fake_ingest, _FakeChat


def _row(*items: tuple[str, str, str | None]) -> dict:
    return {
        "key": "ctx",
        "items": [
            {"id": i, "kind": "turn", "text": t, "tokens": 5, "rank": n, "date": d}
            for n, (i, t, d) in enumerate(items, start=1)
        ],
    }


_D1 = "2023/05/20 (Sat) 02:21"
_D2 = "2023/05/26 (Fri) 07:31"


def test_fragments_of_one_session_are_one_session():
    row = _row(
        ("s1#4u", "User: Bob packed two umbrellas.", _D1),
        ("s2#0u", "User: Tea is fine.", _D2),
        ("s1#0u", "User: Hello there.", _D1),
    )
    text = ea.render_bundle(eb.bundle_from_row(row))
    assert text.count("### Session") == 2
    assert text.count(f"Session Date: {_D1}") == 1
    # Within the session the fragments read in conversation order.
    assert text.index("Hello there") < text.index("two umbrellas")
    assert text.index("### Session 1:") < text.index("two umbrellas") < text.index("### Session 2:")


def test_the_reader_prompt_takes_its_context_from_the_eval_renderer(monkeypatch):
    seen = {}

    def _render(bundle, *, now=None):
        seen["ids"] = [i.id for i in bundle.items]
        seen["now"] = now
        return "RENDERED-BY-THE-EVAL-RENDERER"

    monkeypatch.setattr(ea, "render_bundle", _render)
    bench = _bench(1)
    (q,) = bench.questions
    prompt, _key, _max = eb.answer_prompt_for(bench, q, _row(("s1#0u", "User: hi", _D1)))
    assert "RENDERED-BY-THE-EVAL-RENDERER" in prompt and "### Session" not in prompt
    assert seen == {"ids": ["s1#0u"], "now": q.as_of}
    assert prompt.startswith("I will give you several history chats")


def test_a_different_rendering_is_never_served_a_cached_answer():
    bench = _bench(1)
    (q,) = bench.questions
    ctx = _row(("s1#0u", "User: hi", _D1))
    a = eb._answer_key(ctx, bench, q, "m", prompt="one rendering")
    b = eb._answer_key(ctx, bench, q, "m", prompt="another rendering")
    assert a != b


class _MetaSystem:
    name = "meta_sys"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        item = es.ContextItem(
            "s0#0u", "turn", "User: I took up the cello.", 5, 1, {"source_ref": "bench:h:s0"}
        )
        return es.ContextBundle(self.name, (item,), token_budget, True, False, 1.0)


@pytest.mark.asyncio
async def test_cached_contexts_keep_the_fields_a_renderer_reads(tmp_path):
    import json

    es.register_system("meta_sys", _MetaSystem)
    try:
        await eb.run_benchmark(
            _bench(1),
            systems=["meta_sys"],
            cache_dir=str(tmp_path),
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat(),
            validate_judge=False,
        )
    finally:
        es.unregister_system("meta_sys")
    (row,) = [json.loads(x) for x in (tmp_path / "contexts.jsonl").read_text().splitlines()]
    assert row["items"][0]["meta"] == {"source_ref": "bench:h:s0"}


def test_prompt_version_moved_with_the_rendering():
    assert eb.ANSWER_PROMPT_VERSION >= 2
    assert bd.PromptItem("turn", "x", None).session is None
