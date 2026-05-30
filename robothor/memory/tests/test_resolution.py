"""Tests for WS-4 resolution capture (`robothor.memory.resolution`)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.resolution as res
from robothor.memory.facts import VALID_CATEGORIES, parse_extraction_response
from robothor.memory.resolution import compose_resolution_text, record_resolution


def _mock_conn() -> tuple[Any, Any]:
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


class TestCompose:
    def test_basic(self) -> None:
        assert compose_resolution_text("the X alert", "confirmed safe") == (
            "the X alert — resolved: confirmed safe"
        )

    def test_with_confirmed_by(self) -> None:
        out = compose_resolution_text("the X alert", "safe", "the operator")
        assert out.endswith("(confirmed by the operator)")


class TestRecordResolution:
    @pytest.mark.asyncio
    async def test_requires_fields(self) -> None:
        assert "error" in await record_resolution("", "outcome")
        assert "error" in await record_resolution("item", "")

    @pytest.mark.asyncio
    async def test_stores_and_supersedes_when_enabled(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_RESOLUTION_CAPTURE", "1")
        ctx, cur = _mock_conn()
        supersede = MagicMock()
        with (
            patch.object(res, "store_fact", new=AsyncMock(return_value=500)),
            patch.object(res, "get_connection", return_value=ctx),
            patch.object(
                res,
                "find_similar_facts",
                new=AsyncMock(return_value=[{"id": 10}, {"id": 500}, {"id": 11}]),
            ),
            patch.object(res, "_supersede_fact", new=supersede),
        ):
            out = await record_resolution("the X alert", "confirmed safe", confirmed_by="op")
        assert out["resolution_id"] == 500
        assert out["superseded_ids"] == [10, 11]  # 500 (itself) skipped
        assert supersede.call_count == 2
        # importance bumped on the new fact
        assert any("importance_score" in c.args[0] for c in cur.execute.call_args_list)

    @pytest.mark.asyncio
    async def test_stores_but_no_supersede_when_disabled(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_RESOLUTION_CAPTURE", raising=False)
        ctx, _cur = _mock_conn()
        find = AsyncMock()
        with (
            patch.object(res, "store_fact", new=AsyncMock(return_value=501)),
            patch.object(res, "get_connection", return_value=ctx),
            patch.object(res, "find_similar_facts", new=find),
        ):
            out = await record_resolution("the X alert", "confirmed safe")
        assert out["resolution_id"] == 501
        assert out["superseded_ids"] == []
        find.assert_not_awaited()  # the risky retirement is gated off


class TestExtractionAwareness:
    def test_resolution_is_a_valid_category(self) -> None:
        assert "resolution" in VALID_CATEGORIES

    def test_short_resolution_survives_the_length_filter(self) -> None:
        raw = '[{"fact_text":"Login closed","category":"resolution","entities":["OpenRouter"],"confidence":0.9}]'
        facts = parse_extraction_response(raw)
        assert len(facts) == 1
        assert facts[0]["category"] == "resolution"

    def test_short_non_resolution_still_rejected(self) -> None:
        raw = '[{"fact_text":"X did Y","category":"event","entities":["X"],"confidence":0.9}]'
        assert parse_extraction_response(raw) == []  # < 15 chars, not a resolution


def test_handler_is_registered() -> None:
    from robothor.engine.tools.handlers.memory import HANDLERS

    assert "record_resolution" in HANDLERS
