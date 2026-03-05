"""Tests for robothor.memory.facts — fact extraction and parsing."""

import json

from robothor.memory.facts import (
    VALID_CATEGORIES,
    build_extraction_prompt,
    parse_extraction_response,
)


class TestBuildExtractionPrompt:
    def test_includes_content(self):
        prompt = build_extraction_prompt("John met Alice at the office")
        assert "John met Alice at the office" in prompt

    def test_prompt_has_instructions(self):
        prompt = build_extraction_prompt("test content")
        assert "Extract" in prompt


class TestParseExtractionResponse:
    """Tests for parse_extraction_response.

    The parser has hard quality filters: fact_text must be >=15 chars,
    entities must be non-empty, confidence must be >=0.3, and generic
    patterns are rejected. Test data reflects these constraints.
    """

    def test_valid_json_array(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "John is a senior developer at Acme Corp",
                    "category": "personal",
                    "entities": ["John"],
                    "confidence": 0.9,
                },
            ]
        )
        facts = parse_extraction_response(raw)
        assert len(facts) == 1
        assert facts[0]["fact_text"] == "John is a senior developer at Acme Corp"
        assert facts[0]["category"] == "personal"
        assert facts[0]["entities"] == ["John"]
        assert facts[0]["confidence"] == 0.9

    def test_single_object_wrapped(self):
        raw = json.dumps(
            {
                "fact_text": "Alice reviewed the quarterly report yesterday",
                "category": "technical",
                "entities": ["Alice"],
                "confidence": 0.8,
            }
        )
        facts = parse_extraction_response(raw)
        assert len(facts) == 1

    def test_markdown_fences_stripped(self):
        raw = '```json\n[{"fact_text": "Philip deployed the new memory system", "category": "personal", "entities": ["Philip"], "confidence": 0.9}]\n```'
        facts = parse_extraction_response(raw)
        assert len(facts) == 1

    def test_empty_input(self):
        assert parse_extraction_response("") == []
        assert parse_extraction_response("   ") == []

    def test_invalid_json(self):
        assert parse_extraction_response("not json at all") == []

    def test_missing_fact_text_skipped(self):
        raw = json.dumps(
            [
                {"fact_text": "", "category": "personal", "entities": ["X"], "confidence": 0.9},
                {
                    "fact_text": "Philip completed the migration successfully",
                    "category": "personal",
                    "entities": ["Philip"],
                    "confidence": 0.8,
                },
            ]
        )
        facts = parse_extraction_response(raw)
        assert len(facts) == 1
        assert facts[0]["fact_text"] == "Philip completed the migration successfully"

    def test_invalid_category_defaults(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "Robothor uses PostgreSQL for storage",
                    "category": "invalid_cat",
                    "entities": ["Robothor"],
                    "confidence": 0.8,
                }
            ]
        )
        facts = parse_extraction_response(raw)
        assert facts[0]["category"] == "personal"

    def test_confidence_clamped(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "Alice presented the project roadmap last Friday",
                    "category": "personal",
                    "entities": ["Alice"],
                    "confidence": 1.5,
                },
                {
                    "fact_text": "Bob reviewed the architecture document in detail",
                    "category": "personal",
                    "entities": ["Bob"],
                    "confidence": -0.5,
                },
            ]
        )
        facts = parse_extraction_response(raw)
        # -0.5 clamps to 0.0 which is < 0.3 quality threshold, so only 1 passes
        assert len(facts) == 1
        assert facts[0]["confidence"] == 1.0

    def test_invalid_confidence_defaults(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "Philip configured the SOPS encryption workflow",
                    "category": "personal",
                    "entities": ["Philip"],
                    "confidence": "invalid",
                }
            ]
        )
        facts = parse_extraction_response(raw)
        assert facts[0]["confidence"] == 0.8

    def test_entities_normalized(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "John discussed the deployment plan with team members",
                    "category": "personal",
                    "entities": ["John", 42, None],
                    "confidence": 0.8,
                }
            ]
        )
        facts = parse_extraction_response(raw)
        assert facts[0]["entities"] == ["John", "42"]

    def test_multiple_facts(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "Alice completed the frontend redesign project",
                    "category": "personal",
                    "entities": ["Alice"],
                    "confidence": 0.9,
                },
                {
                    "fact_text": "Bob migrated the database to PostgreSQL 16",
                    "category": "project",
                    "entities": ["Bob"],
                    "confidence": 0.7,
                },
                {
                    "fact_text": "Carol deployed the monitoring stack on Friday",
                    "category": "technical",
                    "entities": ["Carol"],
                    "confidence": 0.6,
                },
            ]
        )
        facts = parse_extraction_response(raw)
        assert len(facts) == 3

    def test_valid_categories(self):
        expected = {
            "personal",
            "project",
            "decision",
            "preference",
            "event",
            "contact",
            "technical",
        }
        assert set(VALID_CATEGORIES) == expected


class TestExtractionSchema:
    def test_schema_structure(self):
        from robothor.memory.facts import FACT_EXTRACTION_SCHEMA

        assert FACT_EXTRACTION_SCHEMA["type"] == "array"
        item_props = FACT_EXTRACTION_SCHEMA["items"]["properties"]
        assert "fact_text" in item_props
        assert "category" in item_props
        assert "entities" in item_props
        assert "confidence" in item_props
