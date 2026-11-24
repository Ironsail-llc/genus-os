"""How a segment row reads once recall returns it: tool output and eval texts."""

from __future__ import annotations

from datetime import UTC, datetime

from robothor.engine.tools.handlers.memory import _format_results

FACT_KEYS = {"id", "fact", "category", "source", "confidence", "similarity", "score"}


def _segment_row() -> dict:
    return {
        "id": 100001,
        "kind": "segment",
        "source": "segment",
        "fact_text": "User: what is the bike locker code?\nAssistant: 4417.",
        "category": "segment",
        "topic": "what is the bike locker code?",
        "source_kind": "conversation",
        "source_ref": "agent:main:primary",
        "start_ref": 11,
        "end_ref": 12,
        "started_at": datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 1, 5, 9, 1, tzinfo=UTC),
        "similarity": 0.8,
        "rrf_score": 0.03,
    }


def test_a_fact_keeps_its_shape():
    row = {"id": 1, "kind": "fact", "fact_text": "Alice prefers tea.", "category": "preference"}
    [out] = _format_results([row], "default")["results"]
    assert set(out) == FACT_KEYS
    assert out["source"] == "fact"


def test_a_segment_says_where_it_came_from():
    [out] = _format_results([_segment_row()], "default")["results"]

    assert out["kind"] == "segment"
    assert out["source"] == "segment"
    assert "4417" in out["fact"]
    assert out["topic"] == "what is the bike locker code?"
    assert out["source_kind"] == "conversation"
    assert out["source_ref"] == "agent:main:primary"
    assert (out["start_ref"], out["end_ref"]) == (11, 12)
    assert out["started_at"] == "2026-01-05T09:00:00+00:00"
    assert out["ended_at"] == "2026-01-05T09:01:00+00:00"


async def test_the_eval_harness_scores_a_segments_text(monkeypatch):
    from robothor.memory import eval as memory_eval
    from robothor.memory import facts

    async def search(*_a, **_kw):
        return [_segment_row()]

    monkeypatch.setattr(facts, "search_facts", search)
    case = memory_eval.EvalCase.__new__(memory_eval.EvalCase)
    object.__setattr__(case, "query", "bike locker code")
    object.__setattr__(case, "k", 5)

    texts = await memory_eval._retrieve(case, "test-tenant")

    assert any("4417" in t for t in texts)
