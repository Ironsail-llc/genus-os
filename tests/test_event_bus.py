"""Tests for robothor.events.bus — uses mock Redis."""

import json
from unittest.mock import MagicMock, patch

import pytest

from robothor.events.bus import (
    VALID_STREAMS,
    _make_envelope,
    _stream_key,
    publish,
    reset_client,
    set_redis_client,
)


@pytest.fixture(autouse=True)
def clean_redis():
    """Reset Redis client between tests."""
    reset_client()
    yield
    reset_client()


class TestStreamKey:
    def test_format(self):
        assert _stream_key("email") == "robothor:events:email"

    def test_all_valid(self):
        for stream in VALID_STREAMS:
            key = _stream_key(stream)
            assert key.startswith("robothor:events:")


class TestMakeEnvelope:
    def test_required_fields(self):
        env = _make_envelope("email.new", {"subject": "Hello"}, source="test")
        assert env["type"] == "email.new"
        assert env["source"] == "test"
        assert env["actor"] == "robothor"
        assert "timestamp" in env
        payload = json.loads(env["payload"])
        assert payload["subject"] == "Hello"

    def test_correlation_id(self):
        env = _make_envelope("test", {}, correlation_id="trace-123")
        assert env["correlation_id"] == "trace-123"

    def test_default_correlation_id(self):
        env = _make_envelope("test", {})
        assert env["correlation_id"] == ""


class TestPublish:
    def test_publish_with_mock_redis(self):
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1234567890-0"
        set_redis_client(mock_redis)

        msg_id = publish("email", "email.new", {"subject": "Test"}, source="test")
        assert msg_id == "1234567890-0"
        mock_redis.xadd.assert_called_once()

    def test_publish_invalid_stream(self):
        """Invalid streams are warned about but still published."""
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1-0"
        set_redis_client(mock_redis)

        msg_id = publish("invalid_stream", "test", {}, source="test")
        assert msg_id == "1-0"
        mock_redis.xadd.assert_called_once()

    @patch("robothor.events.bus.EVENT_BUS_ENABLED", False)
    def test_publish_disabled(self):
        mock_redis = MagicMock()
        set_redis_client(mock_redis)

        msg_id = publish("email", "test", {}, source="test")
        assert msg_id is None

    def test_publish_redis_error(self):
        """Publish gracefully handles Redis errors."""
        mock_redis = MagicMock()
        mock_redis.xadd.side_effect = ConnectionError("Redis down")
        set_redis_client(mock_redis)

        msg_id = publish("email", "test", {}, source="test")
        assert msg_id is None

    def test_valid_streams(self):
        """All 8 expected streams exist."""
        expected = {"email", "calendar", "crm", "vision", "health", "agent", "system", "channel"}
        assert expected == VALID_STREAMS

    def test_publish_all_streams(self):
        """Can publish to every valid stream."""
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1-0"
        set_redis_client(mock_redis)

        for stream in VALID_STREAMS:
            msg_id = publish(stream, f"{stream}.test", {"key": "value"}, source="test")
            assert msg_id == "1-0"


class TestProductionRedisGuard:
    """Regression: tests must never publish to production Redis (db=0)."""

    def test_guard_rejects_db0_under_pytest(self, monkeypatch):
        """_get_redis() must refuse db=0 when PYTEST_CURRENT_TEST is set."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_foo (call)")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        reset_client()
        from robothor.events.bus import _get_redis
        result = _get_redis()
        assert result is None  # guard refuses, returns None via warning path

    def test_guard_allows_nonzero_db(self, monkeypatch):
        """_get_redis() should proceed normally when db != 0 under pytest."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_foo (call)")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
        reset_client()
        # With a mock Redis that responds to ping:
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        set_redis_client(mock_redis)
        from robothor.events.bus import _get_redis
        result = _get_redis()
        assert result is mock_redis

    def test_conftest_pins_test_redis(self):
        """Verify conftest.py sets REDIS_URL to a non-zero db."""
        import os
        url = os.environ.get("REDIS_URL", "")
        if url:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            db_num = int(parsed.path.lstrip("/") or "0")
            assert db_num != 0, f"REDIS_URL should point to test db, got: {url}"
