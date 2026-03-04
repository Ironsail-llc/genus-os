"""
Phase 4: Entity Graph — Tests

Tests for entity extraction, storage, relationships, and querying
the knowledge graph.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from entity_graph import (
    add_relation,
    extract_and_store_entities,
    extract_entities,
    get_all_about,
    get_entity,
    upsert_entity,
)

# ============== Unit Tests: Extract Entities (Mocked LLM) ==============


class TestExtractEntities:
    @pytest.mark.asyncio
    async def test_extract_entities_from_fact(self):
        mock_response = json.dumps(
            {
                "entities": [
                    {"name": "Philip", "type": "person"},
                    {"name": "Neovim", "type": "technology"},
                ],
                "relations": [
                    {"source": "Philip", "target": "Neovim", "relation": "uses"},
                ],
            }
        )
        with patch(
            "entity_graph.llm_client.generate", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await extract_entities("Philip uses Neovim for editing")
            assert len(result["entities"]) == 2
            assert any(e["name"] == "Philip" for e in result["entities"])

    @pytest.mark.asyncio
    async def test_extract_entities_handles_empty(self):
        with patch("entity_graph.llm_client.generate", new_callable=AsyncMock, return_value="{}"):
            result = await extract_entities("")
            assert result["entities"] == []
            assert result["relations"] == []


# ============== Unit Tests: Upsert Entity ==============


class TestUpsertEntity:
    @pytest.mark.asyncio
    async def test_upsert_entity_creates_new(self, test_prefix):
        entity_id = await upsert_entity(f"{test_prefix}_Philip", "person")
        assert isinstance(entity_id, int)
        assert entity_id > 0

    @pytest.mark.asyncio
    async def test_upsert_entity_increments_count(self, test_prefix):
        name = f"{test_prefix}_CountTest"
        id1 = await upsert_entity(name, "person")
        id2 = await upsert_entity(name, "person")
        assert id1 == id2

        import psycopg2
        from entity_graph import DB_CONFIG
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT mention_count FROM memory_entities WHERE id = %s", (id1,))
        assert cur.fetchone()["mention_count"] == 2
        conn.close()


# ============== Unit Tests: Relations ==============


class TestRelations:
    @pytest.mark.asyncio
    async def test_add_relation(self, test_prefix):
        src_id = await upsert_entity(f"{test_prefix}_Alice", "person")
        tgt_id = await upsert_entity(f"{test_prefix}_ProjectX", "project")
        rel_id = await add_relation(src_id, tgt_id, "works_on")
        assert isinstance(rel_id, int)

    @pytest.mark.asyncio
    async def test_add_relation_upserts(self, test_prefix):
        src_id = await upsert_entity(f"{test_prefix}_Bob", "person")
        tgt_id = await upsert_entity(f"{test_prefix}_ProjectY", "project")
        id1 = await add_relation(src_id, tgt_id, "manages")
        id2 = await add_relation(src_id, tgt_id, "manages")
        assert id1 == id2


# ============== Unit Tests: Get Entity ==============


class TestGetEntity:
    @pytest.mark.asyncio
    async def test_get_entity_with_relations(self, test_prefix):
        src_id = await upsert_entity(f"{test_prefix}_Carol", "person")
        tgt_id = await upsert_entity(f"{test_prefix}_Robothor", "project")
        await add_relation(src_id, tgt_id, "contributes_to")

        result = await get_entity(f"{test_prefix}_Carol")
        assert result is not None
        assert result["name"] == f"{test_prefix}_Carol"
        assert len(result["relations"]) >= 1

    @pytest.mark.asyncio
    async def test_get_entity_not_found(self, test_prefix):
        result = await get_entity(f"{test_prefix}_NonexistentEntity12345")
        assert result is None


# ============== Unit Tests: Get All About ==============


class TestGetAllAbout:
    @pytest.mark.asyncio
    async def test_get_all_about_entity(self, test_prefix):
        from fact_extraction import store_fact

        entity_name = f"{test_prefix}_Dave"
        await upsert_entity(entity_name, "person")

        fact = {
            "fact_text": f"{test_prefix} {entity_name} likes hiking in the mountains",
            "category": "preference",
            "entities": [entity_name],
            "confidence": 0.9,
        }
        await store_fact(fact, f"{test_prefix} src", "conversation")

        result = await get_all_about(entity_name)
        assert "entity" in result
        assert "facts" in result


# ============== Integration Tests (Real LLM) ==============


@pytest.mark.slow
class TestIntegrationEntityGraph:
    @pytest.mark.asyncio
    async def test_extract_and_store_entities(self, test_prefix):
        content = f"{test_prefix} Philip decided to use PostgreSQL for the Robothor memory system"
        result = await extract_and_store_entities(content)
        assert "entities_stored" in result
        assert result["entities_stored"] >= 0
