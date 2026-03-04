"""
Tests for the Robothor Event Bus (Redis Streams).

Unit tests use mocked Redis. Integration tests use real Redis.
Tests cover:
- Envelope structure and validation
- Publish to valid/invalid streams
- Subscribe with consumer groups
- Ack/read_recent/stream_info
- Feature flag (EVENT_BUS_ENABLED=false)
- Connection error resilience
- MAXLEN enforcement
"""

import json
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "/home/philip/clawd/memory_system")
import event_bus

# Test stream name — avoids colliding with production
TEST_STREAM = "email"
TEST_STREAM_KEY = f"robothor:events:{TEST_STREAM}"
TEST_GROUP = f"test-group-{uuid.uuid4().hex[:6]}"
TEST_CONSUMER = "test-consumer-1"


@pytest.fixture(autouse=True)
def reset_event_bus():
    """Reset event bus state between tests."""
    event_bus.reset_client()
    # Re-enable feature flag
    event_bus.EVENT_BUS_ENABLED = True
    # Force localhost Redis — earlier test modules may have loaded dotenv with Docker bridge IP
    old_redis_url = os.environ.get("REDIS_URL")
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"
    yield
    event_bus.reset_client()
    if old_redis_url is not None:
        os.environ["REDIS_URL"] = old_redis_url
    else:
        os.environ.pop("REDIS_URL", None)


# ─── Envelope Tests ────────────────────────────────────────────────────


class TestEnvelope:
    def test_envelope_has_required_fields(self):
        env = event_bus._make_envelope(
            "email.new",
            {"subject": "Hello"},
            source="email_sync",
            actor="robothor",
        )
        assert "timestamp" in env
        assert "type" in env
        assert env["type"] == "email.new"
        assert "source" in env
        assert env["source"] == "email_sync"
        assert "actor" in env
        assert env["actor"] == "robothor"
        assert "payload" in env
        assert "correlation_id" in env

    def test_envelope_timestamp_is_iso(self):
        env = event_bus._make_envelope("test.event", {})
        ts = datetime.fromisoformat(env["timestamp"])
        assert ts.tzinfo is not None  # timezone-aware

    def test_envelope_payload_is_json_string(self):
        env = event_bus._make_envelope(
            "test.event",
            {"key": "value", "nested": {"a": 1}},
        )
        parsed = json.loads(env["payload"])
        assert parsed["key"] == "value"
        assert parsed["nested"]["a"] == 1

    def test_envelope_correlation_id(self):
        env = event_bus._make_envelope(
            "test.event",
            {},
            correlation_id="trace-abc-123",
        )
        assert env["correlation_id"] == "trace-abc-123"

    def test_envelope_empty_correlation_id(self):
        env = event_bus._make_envelope("test.event", {})
        assert env["correlation_id"] == ""


# ─── Publish Tests (Mocked Redis) ─────────────────────────────────────


class TestPublishMocked:
    def test_publish_calls_xadd(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xadd.return_value = "1234567890-0"
        event_bus.set_redis_client(mock_redis)

        msg_id = event_bus.publish(
            "email",
            "email.new",
            {"subject": "Test"},
            source="test",
        )
        assert msg_id == "1234567890-0"
        mock_redis.xadd.assert_called_once()
        args, kwargs = mock_redis.xadd.call_args
        assert args[0] == TEST_STREAM_KEY
        assert kwargs["maxlen"] == event_bus.MAXLEN
        assert kwargs["approximate"] is True

    def test_publish_invalid_stream_returns_none(self):
        mock_redis = MagicMock()
        event_bus.set_redis_client(mock_redis)

        msg_id = event_bus.publish("invalid_stream", "test.event", {})
        assert msg_id is None
        mock_redis.xadd.assert_not_called()

    def test_publish_redis_error_returns_none(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xadd.side_effect = Exception("Connection refused")
        event_bus.set_redis_client(mock_redis)

        msg_id = event_bus.publish("email", "email.new", {"test": True})
        assert msg_id is None

    def test_publish_disabled_returns_none(self):
        event_bus.EVENT_BUS_ENABLED = False
        msg_id = event_bus.publish("email", "email.new", {"test": True})
        assert msg_id is None

    def test_publish_no_redis_returns_none(self):
        """When Redis is unavailable, publish returns None."""
        with patch.object(event_bus, "_get_redis", return_value=None):
            msg_id = event_bus.publish("email", "email.new", {})
            assert msg_id is None


# ─── Subscribe Tests (Mocked Redis) ───────────────────────────────────


class TestSubscribeMocked:
    def test_subscribe_creates_group(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xreadgroup.return_value = []
        event_bus.set_redis_client(mock_redis)

        event_bus.subscribe(
            "email",
            TEST_GROUP,
            TEST_CONSUMER,
            handler=lambda e: None,
            max_iterations=1,
        )
        mock_redis.xgroup_create.assert_called_once()

    def test_subscribe_calls_handler(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True

        # Simulate one message
        mock_redis.xreadgroup.side_effect = [
            [
                (
                    TEST_STREAM_KEY,
                    [
                        (
                            "1-0",
                            {
                                "timestamp": "2026-01-01T00:00:00Z",
                                "type": "email.new",
                                "source": "test",
                                "actor": "robothor",
                                "payload": '{"subject": "Hello"}',
                                "correlation_id": "",
                            },
                        ),
                    ],
                )
            ],
            [],  # Second iteration returns empty
        ]
        event_bus.set_redis_client(mock_redis)

        events = []
        event_bus.subscribe(
            "email",
            TEST_GROUP,
            TEST_CONSUMER,
            handler=lambda e: events.append(e),
            max_iterations=2,
        )
        assert len(events) == 1
        assert events[0]["type"] == "email.new"
        assert events[0]["payload"]["subject"] == "Hello"
        assert events[0]["id"] == "1-0"

    def test_subscribe_auto_acks(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xreadgroup.side_effect = [
            [
                (
                    TEST_STREAM_KEY,
                    [
                        (
                            "1-0",
                            {
                                "timestamp": "",
                                "type": "t",
                                "source": "s",
                                "actor": "a",
                                "payload": "{}",
                                "correlation_id": "",
                            },
                        )
                    ],
                )
            ],
            [],
        ]
        event_bus.set_redis_client(mock_redis)

        event_bus.subscribe(
            "email",
            TEST_GROUP,
            TEST_CONSUMER,
            handler=lambda e: None,
            max_iterations=2,
        )
        mock_redis.xack.assert_called_once_with(TEST_STREAM_KEY, TEST_GROUP, "1-0")

    def test_subscribe_handler_error_doesnt_ack(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xreadgroup.side_effect = [
            [
                (
                    TEST_STREAM_KEY,
                    [
                        (
                            "1-0",
                            {
                                "timestamp": "",
                                "type": "t",
                                "source": "s",
                                "actor": "a",
                                "payload": "{}",
                                "correlation_id": "",
                            },
                        )
                    ],
                )
            ],
            [],
        ]
        event_bus.set_redis_client(mock_redis)

        def bad_handler(e):
            raise ValueError("handler failed")

        event_bus.subscribe(
            "email",
            TEST_GROUP,
            TEST_CONSUMER,
            handler=bad_handler,
            max_iterations=2,
        )
        mock_redis.xack.assert_not_called()

    def test_subscribe_disabled_noop(self):
        event_bus.EVENT_BUS_ENABLED = False
        handler = MagicMock()
        event_bus.subscribe("email", "g", "c", handler=handler, max_iterations=1)
        handler.assert_not_called()

    def test_subscribe_existing_group_ok(self):
        """BUSYGROUP error (group already exists) is silently ignored."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xgroup_create.side_effect = Exception("BUSYGROUP group already exists")
        mock_redis.xreadgroup.return_value = []
        event_bus.set_redis_client(mock_redis)

        # Should not raise
        event_bus.subscribe(
            "email",
            TEST_GROUP,
            TEST_CONSUMER,
            handler=lambda e: None,
            max_iterations=1,
        )


# ─── Ack Tests ─────────────────────────────────────────────────────────


class TestAck:
    def test_ack_success(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xack.return_value = 1
        event_bus.set_redis_client(mock_redis)

        result = event_bus.ack("email", "group", "1-0")
        assert result is True

    def test_ack_failure(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xack.side_effect = Exception("Error")
        event_bus.set_redis_client(mock_redis)

        result = event_bus.ack("email", "group", "1-0")
        assert result is False


# ─── Stream Info / Length Tests ────────────────────────────────────────


class TestStreamInfo:
    def test_stream_length(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xlen.return_value = 42
        event_bus.set_redis_client(mock_redis)

        assert event_bus.stream_length("email") == 42

    def test_stream_length_no_redis(self):
        with patch.object(event_bus, "_get_redis", return_value=None):
            assert event_bus.stream_length("email") == 0

    def test_stream_info(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xinfo_stream.return_value = {
            "length": 100,
            "first-entry": ("1-0", {}),
            "last-entry": ("100-0", {}),
            "groups": 2,
        }
        event_bus.set_redis_client(mock_redis)

        info = event_bus.stream_info("email")
        assert info["length"] == 100
        assert info["groups"] == 2


# ─── Read Recent Tests ────────────────────────────────────────────────


class TestReadRecent:
    def test_read_recent_returns_parsed_events(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.xrevrange.return_value = [
            (
                "2-0",
                {
                    "timestamp": "2026-01-01T00:00:01Z",
                    "type": "email.new",
                    "source": "email_sync",
                    "actor": "robothor",
                    "payload": '{"subject": "Second"}',
                    "correlation_id": "",
                },
            ),
            (
                "1-0",
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "email.new",
                    "source": "email_sync",
                    "actor": "robothor",
                    "payload": '{"subject": "First"}',
                    "correlation_id": "",
                },
            ),
        ]
        event_bus.set_redis_client(mock_redis)

        events = event_bus.read_recent("email", count=5)
        assert len(events) == 2
        assert events[0]["payload"]["subject"] == "Second"
        assert events[1]["payload"]["subject"] == "First"

    def test_read_recent_no_redis(self):
        with patch.object(event_bus, "_get_redis", return_value=None):
            assert event_bus.read_recent("email") == []


# ─── Feature Flag Tests ───────────────────────────────────────────────


class TestFeatureFlag:
    def test_disabled_publish(self):
        event_bus.EVENT_BUS_ENABLED = False
        assert event_bus.publish("email", "test", {}) is None

    def test_disabled_subscribe(self):
        event_bus.EVENT_BUS_ENABLED = False
        handler = MagicMock()
        event_bus.subscribe("email", "g", "c", handler=handler)
        handler.assert_not_called()

    def test_enabled_by_default(self):
        # Reset and verify default
        assert event_bus.EVENT_BUS_ENABLED is True


# ─── Integration Tests (Real Redis) ───────────────────────────────────


class TestIntegration:
    """Integration tests using real Redis. Require Redis on localhost:6379."""

    @pytest.fixture(autouse=True)
    def setup_cleanup(self):
        """Use a test-specific stream and clean up after."""
        self.test_stream_name = f"test_{uuid.uuid4().hex[:8]}"
        # Temporarily add our test stream to valid streams
        event_bus.VALID_STREAMS.add(self.test_stream_name)
        yield
        event_bus.cleanup_stream(self.test_stream_name)
        event_bus.VALID_STREAMS.discard(self.test_stream_name)

    @pytest.mark.integration
    def test_publish_and_read_recent(self):
        msg_id = event_bus.publish(
            self.test_stream_name,
            "test.event",
            {"key": "value"},
            source="test",
        )
        assert msg_id is not None

        events = event_bus.read_recent(self.test_stream_name, count=5)
        assert len(events) >= 1
        assert events[0]["type"] == "test.event"
        assert events[0]["payload"]["key"] == "value"

    @pytest.mark.integration
    def test_publish_and_subscribe(self):
        # Publish first
        event_bus.publish(
            self.test_stream_name,
            "test.sub",
            {"data": "hello"},
            source="test",
        )

        # Subscribe and consume
        received = []
        group = f"test-{uuid.uuid4().hex[:6]}"

        event_bus.subscribe(
            self.test_stream_name,
            group,
            "consumer-1",
            handler=lambda e: received.append(e),
            max_iterations=1,
            block_ms=100,
        )
        assert len(received) == 1
        assert received[0]["type"] == "test.sub"
        assert received[0]["payload"]["data"] == "hello"

    @pytest.mark.integration
    def test_stream_length_after_publish(self):
        for i in range(5):
            event_bus.publish(
                self.test_stream_name,
                "test.count",
                {"i": i},
                source="test",
            )
        length = event_bus.stream_length(self.test_stream_name)
        assert length == 5

    @pytest.mark.integration
    def test_stream_info_returns_structure(self):
        event_bus.publish(
            self.test_stream_name,
            "test.info",
            {"x": 1},
            source="test",
        )
        info = event_bus.stream_info(self.test_stream_name)
        assert info is not None
        assert info["length"] >= 1
        assert "first_entry" in info
        assert "last_entry" in info
