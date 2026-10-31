"""Tests for robothor.memory.conflicts — classification schema, prompts and resolution."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robothor.memory.conflicts as conflicts
from robothor.memory.conflicts import (
    CLASSIFICATION_SCHEMA,
    build_classification_prompt,
)


class TestClassificationSchema:
    def test_schema_has_classification(self):
        assert "classification" in CLASSIFICATION_SCHEMA["properties"]
        enum = CLASSIFICATION_SCHEMA["properties"]["classification"]["enum"]
        assert set(enum) == {"new", "duplicate", "update", "contradiction"}

    def test_schema_has_reasoning(self):
        assert "reasoning" in CLASSIFICATION_SCHEMA["properties"]

    def test_required_fields(self):
        assert set(CLASSIFICATION_SCHEMA["required"]) == {"classification", "reasoning"}


class TestBuildClassificationPrompt:
    def test_includes_both_facts(self):
        prompt = build_classification_prompt("New fact text", "Old fact text")
        assert "New fact text" in prompt
        assert "Old fact text" in prompt

    def test_includes_classification_options(self):
        prompt = build_classification_prompt("a", "b")
        assert "duplicate" in prompt
        assert "update" in prompt
        assert "contradiction" in prompt
        assert "new" in prompt


_CONFIRMED = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "operator"}}


def _match(metadata: Any) -> dict[str, Any]:
    return {
        "id": 7,
        "fact_text": "Alice leads the Helios project",
        "category": "personal",
        "similarity": 0.8,
        "metadata": metadata,
    }


async def _resolve(match: dict[str, Any], kind: str, confidence: float) -> tuple[dict, MagicMock]:
    supersede = MagicMock(return_value=True)
    with (
        patch.object(conflicts, "find_similar_facts", new=AsyncMock(return_value=[match])),
        patch.object(
            conflicts,
            "classify_relationship",
            new=AsyncMock(return_value={"classification": kind, "reasoning": "r"}),
        ),
        patch.object(conflicts, "store_fact", new=AsyncMock(return_value=99)),
        patch.object(conflicts, "supersede_with_validity", new=supersede),
        patch.object(conflicts, "record_conflict_decision"),
    ):
        out = await conflicts.resolve_and_store(
            {
                "fact_text": "Bob leads the Helios project",
                "category": "personal",
                "entities": ["Bob"],
                "confidence": confidence,
            },
            "source",
            "conversation",
            tenant_id="test-tenant",
        )
    return out, supersede


class TestOperatorConfirmedSurvivesConflicts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["contradiction", "update"])
    @pytest.mark.parametrize("confidence", [0.5, 1.0])
    async def test_a_confirmed_fact_is_never_superseded(self, kind: str, confidence: float):
        out, supersede = await _resolve(_match(_CONFIRMED), kind, confidence)
        # The new extraction is stored, then recorded as superseded BY the
        # confirmed fact: the pipeline's view is kept, the operator's stays active.
        assert out["new_id"] == 99
        assert out["existing_id"] == 7
        assert out["action"] == "kept_confirmed"
        assert supersede.call_count == 1
        assert supersede.call_args.args[:2] == (99, 7)

    @pytest.mark.asyncio
    async def test_metadata_as_a_json_string_is_still_confirmed(self):
        import json

        out, supersede = await _resolve(_match(json.dumps(_CONFIRMED)), "update", 1.0)
        assert out["action"] == "kept_confirmed"
        assert supersede.call_args.args[:2] == (99, 7)

    @pytest.mark.asyncio
    async def test_an_unconfirmed_fact_is_still_superseded(self):
        out, supersede = await _resolve(_match({}), "update", 1.0)
        assert out["action"] == "superseded"
        assert supersede.call_args.args[:2] == (7, 99)

    @pytest.mark.asyncio
    async def test_similar_facts_carry_their_metadata(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cur
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = False
        with (
            patch.object(
                conflicts.llm_client, "get_embedding_async", new=AsyncMock(return_value=[0.0])
            ),
            patch.object(conflicts, "get_connection", return_value=ctx),
            patch.object(conflicts, "apply_hnsw_session"),
        ):
            await conflicts.find_similar_facts("anything", tenant_id="test-tenant")
        select = cur.execute.call_args.args[0]
        assert "metadata" in select.split("FROM")[0]
