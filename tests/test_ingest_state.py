"""Tests for robothor.memory.ingest_state — content hashing (pure unit tests)."""

from robothor.memory.ingest_state import content_hash


class TestContentHash:
    def test_deterministic(self):
        data = {"subject": "Hello", "body": "World"}
        h1 = content_hash(data, ["subject", "body"])
        h2 = content_hash(data, ["subject", "body"])
        assert h1 == h2

    def test_different_data_different_hash(self):
        h1 = content_hash({"subject": "Hello"}, ["subject"])
        h2 = content_hash({"subject": "World"}, ["subject"])
        assert h1 != h2

    def test_key_order_doesnt_matter(self):
        """Keys are sorted internally, so order doesn't matter."""
        data = {"b": "2", "a": "1"}
        h1 = content_hash(data, ["a", "b"])
        h2 = content_hash(data, ["b", "a"])
        assert h1 == h2

    def test_missing_key_uses_empty(self):
        h1 = content_hash({"subject": "Hello"}, ["subject", "missing"])
        h2 = content_hash({"subject": "Hello", "missing": ""}, ["subject", "missing"])
        assert h1 == h2

    def test_none_value_treated_as_empty(self):
        h1 = content_hash({"key": None}, ["key"])
        h2 = content_hash({"key": ""}, ["key"])
        assert h1 == h2

    def test_returns_64_char_hex(self):
        h = content_hash({"a": "b"}, ["a"])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
