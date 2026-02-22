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
    def test_valid_json_array(self):
        raw = json.dumps([
            {"fact_text": "John is a developer", "category": "personal", "entities": ["John"], "confidence": 0.9},
        ])
        facts = parse_extraction_response(raw)
        assert len(facts) == 1
        assert facts[0]["fact_text"] == "John is a developer"
        assert facts[0]["category"] == "personal"
        assert facts[0]["entities"] == ["John"]
        assert facts[0]["confidence"] == 0.9

    def test_single_object_wrapped(self):
        raw = json.dumps({"fact_text": "Test fact", "category": "technical", "entities": [], "confidence": 0.8})
        facts = parse_extraction_response(raw)
        assert len(facts) == 1

    def test_markdown_fences_stripped(self):
        raw = '```json\n[{"fact_text": "Test", "category": "personal", "entities": [], "confidence": 0.9}]\n```'
        facts = parse_extraction_response(raw)
        assert len(facts) == 1

    def test_empty_input(self):
        assert parse_extraction_response("") == []
        assert parse_extraction_response("   ") == []

    def test_invalid_json(self):
        assert parse_extraction_response("not json at all") == []

    def test_missing_fact_text_skipped(self):
        raw = json.dumps([
            {"fact_text": "", "category": "personal", "entities": [], "confidence": 0.9},
            {"fact_text": "Valid fact", "category": "personal", "entities": [], "confidence": 0.8},
        ])
        facts = parse_extraction_response(raw)
        assert len(facts) == 1
        assert facts[0]["fact_text"] == "Valid fact"

    def test_invalid_category_defaults(self):
        raw = json.dumps([{"fact_text": "Test", "category": "invalid_cat", "entities": [], "confidence": 0.8}])
        facts = parse_extraction_response(raw)
        assert facts[0]["category"] == "personal"

    def test_confidence_clamped(self):
        raw = json.dumps([
            {"fact_text": "High", "category": "personal", "entities": [], "confidence": 1.5},
            {"fact_text": "Low", "category": "personal", "entities": [], "confidence": -0.5},
        ])
        facts = parse_extraction_response(raw)
        assert facts[0]["confidence"] == 1.0
        assert facts[1]["confidence"] == 0.0

    def test_invalid_confidence_defaults(self):
        raw = json.dumps([{"fact_text": "Test", "category": "personal", "entities": [], "confidence": "invalid"}])
        facts = parse_extraction_response(raw)
        assert facts[0]["confidence"] == 0.8

    def test_entities_normalized(self):
        raw = json.dumps([{"fact_text": "Test", "category": "personal", "entities": ["John", 42, None], "confidence": 0.8}])
        facts = parse_extraction_response(raw)
        assert facts[0]["entities"] == ["John", "42"]

    def test_multiple_facts(self):
        raw = json.dumps([
            {"fact_text": "Fact 1", "category": "personal", "entities": ["A"], "confidence": 0.9},
            {"fact_text": "Fact 2", "category": "project", "entities": ["B"], "confidence": 0.7},
            {"fact_text": "Fact 3", "category": "technical", "entities": [], "confidence": 0.6},
        ])
        facts = parse_extraction_response(raw)
        assert len(facts) == 3

    def test_valid_categories(self):
        expected = {"personal", "project", "decision", "preference", "event", "contact", "technical"}
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
