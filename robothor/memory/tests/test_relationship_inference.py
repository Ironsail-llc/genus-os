"""Tests for cross-fact entity relationship inference."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory.entities import (
    find_cooccurring_entity_pairs,
    find_underconnected_entities,
    infer_relations,
)


@pytest.fixture
def mock_db():
    """Mock get_connection for DB operations."""
    with patch("robothor.memory.entities.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        mock_conn.return_value = conn
        yield cur


# ── find_underconnected_entities ─────────────────────────────────────────────


class TestFindUnderconnectedEntities:
    @pytest.mark.asyncio
    async def test_returns_entities_with_mentions_but_few_relations(self, mock_db):
        mock_db.fetchall.return_value = [
            {
                "id": 1,
                "name": "Alice",
                "entity_type": "person",
                "mention_count": 5,
                "relation_count": 0,
            },
            {
                "id": 2,
                "name": "Acme Corp",
                "entity_type": "organization",
                "mention_count": 3,
                "relation_count": 1,
            },
        ]
        result = await find_underconnected_entities(min_mentions=2, max_relations=1)
        assert len(result) == 2
        assert result[0]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_underconnected(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await find_underconnected_entities()
        assert result == []

    @pytest.mark.asyncio
    async def test_respects_limit(self, mock_db):
        mock_db.fetchall.return_value = [
            {
                "id": i,
                "name": f"Entity{i}",
                "entity_type": "person",
                "mention_count": 3,
                "relation_count": 0,
            }
            for i in range(5)
        ]
        result = await find_underconnected_entities(limit=5)
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_sql_uses_correct_parameters(self, mock_db):
        mock_db.fetchall.return_value = []
        await find_underconnected_entities(min_mentions=3, max_relations=2, limit=10)
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert 3 in params  # min_mentions
        assert 2 in params  # max_relations
        assert 10 in params  # limit


# ── find_cooccurring_entity_pairs ────────────────────────────────────────────


class TestFindCooccurringEntityPairs:
    @pytest.mark.asyncio
    async def test_returns_pairs_with_shared_facts(self, mock_db):
        mock_db.fetchall.return_value = [
            {
                "entity_a_id": 1,
                "entity_a_name": "Alice",
                "entity_b_id": 2,
                "entity_b_name": "Acme Corp",
                "shared_fact_count": 3,
                "shared_fact_ids": [10, 11, 12],
            },
        ]
        result = await find_cooccurring_entity_pairs([1, 2, 3])
        assert len(result) == 1
        assert result[0]["entity_a_name"] == "Alice"
        assert result[0]["shared_fact_count"] == 3

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_cooccurrences(self, mock_db):
        mock_db.fetchall.return_value = []
        result = await find_cooccurring_entity_pairs([1, 2])
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_entity_ids(self):
        # Should short-circuit without hitting DB
        result = await find_cooccurring_entity_pairs([])
        assert result == []


# ── infer_relations ──────────────────────────────────────────────────────────


class TestInferRelations:
    @pytest.mark.asyncio
    async def test_stores_inferred_relations_with_low_confidence(self):
        pairs = [
            {
                "entity_a_id": 1,
                "entity_a_name": "Alice",
                "entity_b_id": 2,
                "entity_b_name": "Acme Corp",
                "shared_fact_ids": [10, 11],
                "shared_facts_text": [
                    "Alice joined Acme Corp as VP Engineering",
                    "Acme Corp promoted Alice to CTO",
                ],
            },
        ]
        llm_response = json.dumps(
            {
                "relations": [
                    {
                        "source": "Alice",
                        "target": "Acme Corp",
                        "relation": "works_at",
                        "confidence": 0.8,
                    }
                ]
            }
        )

        with (
            patch(
                "robothor.memory.entities.llm_client.generate",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
            patch(
                "robothor.memory.entities.add_relation",
                new_callable=AsyncMock,
                return_value=100,
            ) as mock_add_rel,
        ):
            result = await infer_relations(pairs)
            assert len(result) == 1
            assert result[0]["relation_type"] == "works_at"
            # Confidence should be capped at 0.7 for inferred relations
            call_kwargs = mock_add_rel.call_args
            assert call_kwargs[0][4] <= 0.7  # confidence arg

    @pytest.mark.asyncio
    async def test_caps_confidence_at_0_7(self):
        pairs = [
            {
                "entity_a_id": 1,
                "entity_a_name": "X",
                "entity_b_id": 2,
                "entity_b_name": "Y",
                "shared_fact_ids": [1],
                "shared_facts_text": ["X uses Y daily"],
            },
        ]
        llm_response = json.dumps(
            {"relations": [{"source": "X", "target": "Y", "relation": "uses", "confidence": 0.95}]}
        )

        with (
            patch(
                "robothor.memory.entities.llm_client.generate",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
            patch(
                "robothor.memory.entities.add_relation",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_add_rel,
        ):
            await infer_relations(pairs)
            confidence = mock_add_rel.call_args[0][4]
            assert confidence <= 0.7

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_pairs(self):
        result = await infer_relations([])
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_llm_failure_gracefully(self):
        pairs = [
            {
                "entity_a_id": 1,
                "entity_a_name": "A",
                "entity_b_id": 2,
                "entity_b_name": "B",
                "shared_fact_ids": [1],
                "shared_facts_text": ["A and B work together"],
            },
        ]
        with patch(
            "robothor.memory.entities.llm_client.generate",
            new_callable=AsyncMock,
            side_effect=Exception("LLM timeout"),
        ):
            result = await infer_relations(pairs)
            assert result == []

    @pytest.mark.asyncio
    async def test_skips_relations_with_unknown_entities(self):
        """LLM might return entity names that don't match the pair."""
        pairs = [
            {
                "entity_a_id": 1,
                "entity_a_name": "Alice",
                "entity_b_id": 2,
                "entity_b_name": "Bob",
                "shared_fact_ids": [1],
                "shared_facts_text": ["Alice and Bob had a meeting"],
            },
        ]
        llm_response = json.dumps(
            {
                "relations": [
                    {"source": "Charlie", "target": "Dave", "relation": "knows", "confidence": 0.8}
                ]
            }
        )

        with (
            patch(
                "robothor.memory.entities.llm_client.generate",
                new_callable=AsyncMock,
                return_value=llm_response,
            ),
            patch(
                "robothor.memory.entities.add_relation",
                new_callable=AsyncMock,
            ) as mock_add_rel,
        ):
            result = await infer_relations(pairs)
            assert result == []
            mock_add_rel.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_malformed_llm_json(self):
        pairs = [
            {
                "entity_a_id": 1,
                "entity_a_name": "A",
                "entity_b_id": 2,
                "entity_b_name": "B",
                "shared_fact_ids": [1],
                "shared_facts_text": ["A and B"],
            },
        ]
        with patch(
            "robothor.memory.entities.llm_client.generate",
            new_callable=AsyncMock,
            return_value="not valid json {{{",
        ):
            result = await infer_relations(pairs)
            assert result == []
