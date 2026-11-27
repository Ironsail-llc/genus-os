"""The earlier value reaches the reader labelled, next to its current value.

``search_facts`` follows a current row that replaced a value with that earlier
row (``superseded``, ``superseded_at``, ``superseded_reason``,
``superseded_by``, and ``earlier_of`` naming its current row). Every surface
that shows rows to a model must keep the label, keep the earlier row directly
after its current row, drop it when its current row is cut, and never let it
take a ranked row's place.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory.bitemporal import MAX_EARLIER_ROWS, place_superseded
from robothor.memory.recall import RECALL_CONTEXT_CHARS, row_text, within_context_cap

_NOW = dt.datetime(2026, 11, 27, tzinfo=dt.UTC)
_WHEN = dt.datetime(2026, 5, 1, tzinfo=dt.UTC)


def _cur(fid: int, text: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": fid,
        "kind": "fact",
        "fact_text": text,
        "category": "personal",
        "confidence": 0.9,
        "similarity": 0.8,
        "rrf_score": 0.03,
        "fused_rank": 0,
        "rerank_score": None,
        "leg_ranks": {"vector": 0},
        "leg_scores": {"vector": 0.8},
        "relevance": 0.9,
        "created_at": _NOW - dt.timedelta(days=1),
        "superseded_by": None,
        **kw,
    }


def _old(fid: int, text: str, by: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": fid,
        "kind": "fact",
        "fact_text": text,
        "category": "personal",
        "confidence": 0.9,
        "created_at": _NOW - dt.timedelta(days=400),
        "superseded_by": by,
        "valid_to": _WHEN,
        "metadata": {"superseded_reason": "changed"},
        **kw,
    }


def _pair() -> list[dict[str, Any]]:
    """search_facts' output for 'Alice moved to Denver' replacing Boston."""
    return place_superseded(
        [_cur(2, "Alice moved to Denver.", metadata={"supersedes": [1]})],
        earlier={2: [_old(1, "Alice lives in Boston.", 2)]},
    )


class TestTheLabel:
    def test_row_text_says_it_is_the_earlier_value_and_when_it_was_replaced(self):
        old = _pair()[1]
        assert row_text(old) == "Earlier value, replaced 2026-05-01: Alice lives in Boston."

    def test_the_current_row_reads_plainly(self):
        assert row_text(_pair()[0]) == "Alice moved to Denver."


class TestThePlacedRow:
    def test_an_earlier_row_has_the_full_row_shape_with_null_scores(self):
        cur, old = _pair()
        assert set(cur) - {"metadata"} <= set(old)
        for key in ("similarity", "rerank_score", "rrf_score", "fused_rank"):
            assert old[key] is None
        assert old["leg_scores"] == {}
        assert old["leg_ranks"] == cur["leg_ranks"]
        assert old["earlier_of"] == 2

    def test_it_inherits_the_current_rows_position_markers(self):
        rows = place_superseded(
            [_cur(2, "Alice moved to Denver.", budget_tail=True)],
            earlier={2: [_old(1, "Alice lives in Boston.", 2)]},
        )
        assert rows[1]["budget_tail"] is True

    def test_a_cycle_of_closed_rows_does_not_crash(self):
        rows = place_superseded([_old(1, "A.", 2), _old(2, "B.", 1)])
        assert sorted(r["id"] for r in rows) == [1, 2]

    def test_earlier_rows_are_bounded(self):
        currents = [_cur(10 + i, f"Person{i} moved to Denver.") for i in range(6)]
        earlier = {10 + i: [_old(20 + i, f"Person{i} lives in Boston.", 10 + i)] for i in range(6)}
        rows = place_superseded(currents, earlier=earlier)
        assert len(rows) == 6 + MAX_EARLIER_ROWS
        assert [r["id"] for r in rows if not r.get("superseded")] == [10 + i for i in range(6)]

    def test_an_earlier_row_duplicating_another_kept_row_is_dropped(self):
        rows = place_superseded(
            [_cur(2, "Alice moved to Denver."), _cur(3, "Alice lives in Boston.")],
            earlier={2: [_old(1, "Alice lives in Boston.", 2)]},
        )
        assert [r["id"] for r in rows] == [2, 3]

    def test_a_reversal_is_kept_next_to_its_own_current_row(self):
        rows = place_superseded(
            [_cur(2, "Bob prefers coffee over tea.")],
            earlier={2: [_old(1, "Bob prefers tea over coffee.", 2)]},
        )
        assert [r["id"] for r in rows] == [2, 1]


class TestPacking:
    def test_an_earlier_row_never_survives_without_its_current_row(self):
        long_current = _cur(2, "Alice moved to Denver " + "x" * RECALL_CONTEXT_CHARS)
        head = _cur(5, "Bob visited Paris.")
        rows = place_superseded(
            [head, long_current], earlier={2: [_old(1, "Alice lives in Boston.", 2)]}
        )
        packed = within_context_cap(rows)
        assert 1 not in [r["id"] for r in packed]

    def test_an_earlier_row_never_displaces_a_ranked_row(self):
        size = RECALL_CONTEXT_CHARS // 2 - 1
        rows = place_superseded(
            [_cur(2, "D" * size), _cur(3, "E" * size)],
            earlier={2: [_old(1, "Alice lives in Boston.", 2)]},
        )
        assert [r["id"] for r in within_context_cap(rows)] == [2, 3]

    def test_an_earlier_row_that_fits_follows_its_current_row(self):
        assert [r["id"] for r in within_context_cap(_pair())] == [2, 1]


class TestTheTool:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("rip15", ["", "1"])
    async def test_search_memory_shows_the_label_and_the_fields(self, monkeypatch, rip15):
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers import memory as memmod

        monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", rip15)
        ctx = ToolContext(tenant_id="test-tenant")
        with patch("robothor.memory.facts.search_facts", AsyncMock(return_value=_pair())):
            out = await memmod.HANDLERS["search_memory"]({"query": "Where does Alice live?"}, ctx)
        rows = out["results"]
        assert [r["id"] for r in rows] == [2, 1]
        old = rows[1]
        assert old["fact"].startswith("Earlier value, replaced 2026-05-01: ")
        assert old["superseded"] is True
        assert old["superseded_at"].startswith("2026-05-01")
        assert old["superseded_by"] == 2
        assert old["superseded_reason"] == "changed"
        assert not rows[0].get("superseded")

    @pytest.mark.asyncio
    async def test_mcp_search_memory_shows_the_label_and_the_fields(self, monkeypatch):
        from robothor.api.mcp import handle_tool_call

        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "test-tenant")
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "test-tenant")
        with patch("robothor.memory.facts.search_facts", AsyncMock(return_value=_pair())):
            out = await handle_tool_call("search_memory", {"query": "Where does Alice live?"})
        old = out["results"][1]
        assert old["fact"].startswith("Earlier value, replaced 2026-05-01: ")
        assert (old["superseded"], old["superseded_by"]) == (True, 2)
        assert old["superseded_at"].startswith("2026-05-01")


class TestTheRouter:
    @staticmethod
    async def _recall(rows: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
        from robothor.memory import router

        with patch("robothor.memory.facts.search_facts", AsyncMock(return_value=rows)):
            out = await router.recall(query, tenant_id="test-tenant", limit=limit)
        return out["results"]

    @pytest.mark.asyncio
    async def test_a_recency_sort_keeps_the_earlier_row_after_its_current_one(self):
        head = _cur(5, "Bob visited Paris last week.", created_at=_NOW - dt.timedelta(days=3))
        current = _cur(2, "Alice moved to Denver.", created_at=_NOW - dt.timedelta(days=10))
        rows = place_superseded(
            [head, current], earlier={2: [_old(1, "Alice lives in Boston.", 2)]}
        )
        out = await self._recall(rows, "What happened last week with Alice?", 10)
        ids = [r["id"] for r in out]
        assert ids.index(1) == ids.index(2) + 1
        assert out[ids.index(1)]["superseded"] is True

    @pytest.mark.asyncio
    async def test_a_graph_only_current_row_keeps_its_earlier_row_after_it(self):
        head = _cur(5, "Bob visited Paris last week.")
        current = _cur(2, "Alice moved to Denver.", leg_ranks={"graph": 0})
        rows = place_superseded(
            [head, current], earlier={2: [_old(1, "Alice lives in Boston.", 2)]}
        )
        out = await self._recall(rows, "What happened last week with Alice?", 10)
        ids = [r["id"] for r in out]
        assert ids.index(1) == ids.index(2) + 1

    @pytest.mark.asyncio
    async def test_an_earlier_row_never_pushes_a_ranked_row_past_the_limit(self):
        current = _cur(2, "Alice moved to Denver.")
        many = [_cur(100 + i, f"Fact {i}.") for i in range(4)]
        rows = place_superseded(
            [current, *many], earlier={2: [_old(1, "Alice lives in Boston.", 2)]}
        )
        out = await self._recall(rows, "Where does Alice live", 5)
        ids = [r["id"] for r in out]
        assert [i for i in ids if i != 1] == [2, 100, 101, 102, 103]
        assert ids.index(1) == ids.index(2) + 1
