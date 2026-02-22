"""Tests for robothor.memory.entities — entity schemas and types."""

from robothor.memory.entities import (
    ENTITY_EXTRACTION_SCHEMA,
    VALID_ENTITY_TYPES,
)


class TestEntityTypes:
    def test_valid_types(self):
        expected = {"person", "project", "organization", "technology", "location", "event"}
        assert set(VALID_ENTITY_TYPES) == expected


class TestExtractionSchema:
    def test_schema_has_entities_and_relations(self):
        assert "entities" in ENTITY_EXTRACTION_SCHEMA["properties"]
        assert "relations" in ENTITY_EXTRACTION_SCHEMA["properties"]

    def test_entity_requires_name_and_type(self):
        entity_schema = ENTITY_EXTRACTION_SCHEMA["properties"]["entities"]["items"]
        assert "name" in entity_schema["properties"]
        assert "type" in entity_schema["properties"]
        assert set(entity_schema["required"]) == {"name", "type"}

    def test_relation_requires_source_target_relation(self):
        rel_schema = ENTITY_EXTRACTION_SCHEMA["properties"]["relations"]["items"]
        assert "source" in rel_schema["properties"]
        assert "target" in rel_schema["properties"]
        assert "relation" in rel_schema["properties"]
        assert set(rel_schema["required"]) == {"source", "target", "relation"}
