"""Tests for robothor.memory.conflicts — classification schema and prompts."""

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
