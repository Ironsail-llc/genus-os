"""A changed value on the paths around recall and the write: the memory
evaluation packs earlier values as the tool does, and the write path counts a
value filed as history apart from a live one."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory import eval_systems as es

_CHARS = es._CHARS_PER_TOKEN


def _text(tokens: int, word: str = "x") -> str:
    return (word * (tokens * _CHARS))[: tokens * _CHARS]


class TestTheEvalPacksLikeTheTool:
    def test_an_earlier_value_whose_current_value_does_not_fit_is_left_out(self):
        cands = [
            ("head", "fact", _text(5)),
            ("current", "fact", _text(50)),
            ("earlier", "fact", "Earlier value, replaced 2026-05-01: Alice lived in Boston.", "current"),
        ]  # fmt: skip
        ids = [i.id for i in es.fill_budget(cands, 20)]
        assert "earlier" not in ids

    def test_an_earlier_value_follows_its_current_value_and_displaces_nothing(self):
        cands = [
            ("current", "fact", _text(5)),
            ("earlier", "fact", _text(5, "e"), "current"),
            ("other", "fact", _text(10)),
        ]
        assert [i.id for i in es.fill_budget(cands, 15)] == ["current", "other"]
        assert [i.id for i in es.fill_budget(cands, 25)] == ["current", "earlier", "other"]

    def test_fact_candidates_carry_which_fact_an_earlier_value_follows(self):
        handle = MagicMock()
        handle.fact_id.side_effect = {2: "w-current", 1: "w-earlier"}.get
        rows = [
            {"id": 2, "kind": "fact", "fact_text": "Alice moved to Denver."},
            {
                "id": 1,
                "kind": "fact",
                "fact_text": "Alice lives in Boston.",
                "superseded": True,
                "superseded_at": "2026-05-01",
                "earlier_of": 2,
            },
        ]
        cands = es._fact_candidates(rows, handle)
        assert cands[0][:2] == ("w-current", "fact")
        assert cands[1][0] == "w-earlier" and cands[1][3] == "w-current"
        assert cands[1][2].startswith("Earlier value, replaced 2026-05-01: ")


class TestTheWritePath:
    @pytest.mark.asyncio
    async def test_a_value_filed_as_history_is_counted_and_writes_no_graph(self):
        import robothor.memory.write_path as wp
        from robothor.memory.graph_schema import Origin

        facts = [
            {"fact_text": "Alice moved to Denver in 2019.", "category": "personal"},
            {"fact_text": "Bob joined the Helios team.", "category": "personal"},
        ]
        results: list[dict[str, Any]] = [
            {"action": "stored_history", "new_id": 99, "current_id": 7},
            {"action": "stored", "new_id": 100},
        ]
        graph = MagicMock(return_value={"mentions_written": 0, "relations_stored": 0})
        ctx = wp.WriteContext(
            tenant_id="test-tenant",
            source_channel="chat",
            content_type="conversation",
            origin=Origin.USER_STATED,
        )
        with (
            patch.object(wp, "resolve_and_store", new=AsyncMock(side_effect=results)),
            patch.object(wp, "_write_graph", new=graph),
            patch.object(wp, "_embed", new=AsyncMock(return_value=[[0.0], [0.0]])),
        ):
            out = await wp.write_facts(facts, source_content="content", context=ctx)
        assert out["fact_ids"] == [100]
        assert out["facts_stored_history"] == 1
        assert out["history_ids"] == [99]
        assert out["facts_skipped"] == 0
        # A past value's relations would read as current edges: none written.
        assert [c.args[1] for c in graph.call_args_list] == [100]
