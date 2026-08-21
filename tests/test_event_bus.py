"""Tests for robothor.events.bus — uses mock Redis."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from robothor.events import bus
from robothor.events.bus import (
    VALID_STREAMS,
    EventBusGuardError,
    _allowed_namespaces,
    _get_redis,
    _make_envelope,
    _namespace_from_url,
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


# Namespaces used by the guard tests. ``.invalid`` is reserved by RFC 2606 and
# never resolves, so a guard regression cannot silently reach a real host.
PROD_LOCAL_URL = "redis://localhost:6379/0"
PROD_REMOTE_URL = "redis://redis.prod.example.invalid:6379/3"
PROD_REMOTE_NS = "redis.prod.example.invalid:6379/3"
TEST_URL = "redis://localhost:6379/15"


class TestEventBusGuard:
    """The test suite must never publish onto a production event bus.

    Between 2026-03-02 and 2026-08-21 it did: ``robothor:events:vision`` held
    1346 entries of which 601 were synthetic ``camera="test-camera"`` payloads,
    and ``robothor:events:crm`` was polluted by tests/test_operator_identity.py.
    The live engine consumed every one of them as a genuine hook.
    """

    def test_publish_to_non_allowlisted_namespace_raises(self, monkeypatch):
        """Publishing outside the test allowlist is a hard failure, not a log line.

        The message must name the offending namespace and the escape hatch, so
        whoever hits it can act without reading this module.
        """
        monkeypatch.setenv("REDIS_URL", PROD_LOCAL_URL)
        reset_client()

        with pytest.raises(EventBusGuardError) as exc:
            publish("vision", "vision.person", {"camera": "test-camera"}, source="test")

        message = str(exc.value)
        assert "localhost:6379/0" in message
        assert "ROBOTHOR_EVENT_BUS_ALLOW" in message

    def test_guard_error_is_not_catchable_by_except_exception(self):
        """The guard must sit outside the ``Exception`` hierarchy.

        This is the invariant the whole guard rests on, so it is asserted
        directly rather than inferred. Every producer wraps its publish in a
        best-effort ``except Exception: logger.warning(...)``; an
        ``Exception``-derived guard is swallowed by all of them and the run
        stays green. Fails the moment someone tidies this back into a
        RuntimeError.
        """
        assert issubclass(EventBusGuardError, BaseException)
        assert not issubclass(EventBusGuardError, Exception)

    def test_guard_survives_a_call_site_that_swallows_everything(self, monkeypatch):
        """A producer's best-effort handler must not be able to eat the guard.

        Reproduces the real shape of robothor/crm/dal.py:2349 and every other
        publisher in the codebase. Measured, not assumed: with an
        Exception-derived guard, aiming tests/test_operator_identity.py at
        production Redis logged "Failed to publish task.resolved event" twice
        and the run still reported 20 passed.
        """
        monkeypatch.setenv("REDIS_URL", PROD_LOCAL_URL)
        reset_client()
        swallowed = False

        def producer_with_best_effort_publish():
            nonlocal swallowed
            try:
                publish("crm", "task.resolved", {"task_id": "t-1"}, source="crm_dal")
            except Exception:  # noqa: BLE001 — verbatim shape of the real call sites
                swallowed = True

        with pytest.raises(EventBusGuardError):
            producer_with_best_effort_publish()
        assert swallowed is False

    def test_get_redis_refuses_before_building_a_client(self, monkeypatch):
        """The guard fires OUTSIDE _get_redis()'s ``except Exception``.

        Two properties in one: an off-allowlist destination raises, and no
        Redis client is ever constructed for it. The first pass put the raise
        inside that ``try``, where the handler that exists to downgrade "Redis
        is down" to a warning and a ``None`` return downgraded "this test is
        about to write to production" to exactly the same thing.
        """
        import redis

        monkeypatch.setenv("REDIS_URL", PROD_LOCAL_URL)
        reset_client()
        from_url = MagicMock()
        monkeypatch.setattr(redis.Redis, "from_url", from_url)

        with pytest.raises(EventBusGuardError):
            _get_redis()
        from_url.assert_not_called()

    def test_injected_production_client_is_caught_at_publish_boundary(self, monkeypatch):
        """A client injected past _get_redis() is still caught before the XADD.

        set_redis_client() is public, so a test can build its own client from
        any DSN and REDIS_URL then describes nothing. A guard that only reads
        the environment is blind to this — the same re-exported-symbol evasion
        that defeated the database pin for months (PR #300). The guard must read
        the destination of the client that would actually receive the write.
        """
        import redis

        monkeypatch.setenv("REDIS_URL", TEST_URL)  # environment says "safe"
        prod_client = redis.Redis.from_url(PROD_REMOTE_URL, decode_responses=True)
        prod_client.ping = lambda: True  # never touch the network
        prod_client.xadd = MagicMock()
        set_redis_client(prod_client)

        with pytest.raises(EventBusGuardError) as exc:
            publish("vision", "vision.person", {"camera": "test-camera"}, source="test")

        assert PROD_REMOTE_NS in str(exc.value)
        prod_client.xadd.assert_not_called()

    def test_non_zero_production_db_is_rejected(self, monkeypatch):
        """The allowlist is positive: a prod Redis on another host/db is refused.

        Fails if the guard reverts to blocklisting ``db == 0``, which let every
        other host and every non-zero production database straight through.
        """
        monkeypatch.setenv("REDIS_URL", PROD_REMOTE_URL)
        reset_client()

        with pytest.raises(EventBusGuardError) as exc:
            publish("system", "system.boot", {}, source="test")

        assert PROD_REMOTE_NS in str(exc.value)

    def test_escape_hatch_allows_one_exact_namespace(self, monkeypatch):
        """ROBOTHOR_EVENT_BUS_ALLOW opts a named namespace back in.

        Mirrors ROBOTHOR_TEST_DB_ALLOW in robothor/db/connection.py, which the
        release gate uses to run integration tests against a non-``*_test``
        database. Asserts both halves so it cannot pass by the guard's absence.
        """
        monkeypatch.setenv("REDIS_URL", PROD_REMOTE_URL)
        monkeypatch.delenv("ROBOTHOR_EVENT_BUS_ALLOW", raising=False)
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1-0"

        set_redis_client(mock_redis)
        with pytest.raises(EventBusGuardError):
            publish("system", "system.boot", {}, source="test")

        monkeypatch.setenv("ROBOTHOR_EVENT_BUS_ALLOW", PROD_REMOTE_NS)
        set_redis_client(mock_redis)
        assert publish("system", "system.boot", {}, source="test") == "1-0"

    def test_guard_is_a_noop_outside_pytest(self, monkeypatch):
        """Production must be able to publish to production.

        Asserts both halves — the same call raises under pytest and succeeds
        outside it — so the test cannot pass by the guard doing nothing at all.
        """
        monkeypatch.setenv("REDIS_URL", PROD_LOCAL_URL)
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1-0"

        set_redis_client(mock_redis)
        with pytest.raises(EventBusGuardError):
            publish("system", "system.boot", {}, source="test")

        monkeypatch.setattr(bus, "_in_pytest", lambda: False)
        set_redis_client(mock_redis)
        assert publish("system", "system.boot", {}, source="test") == "1-0"

    def test_allowed_test_namespace_publishes_normally(self, monkeypatch):
        """Positive control: the sanctioned test namespace still works.

        Green before and after by design — its job is to go red if the guard is
        ever tightened into blocking the namespace tests are supposed to use.
        """
        monkeypatch.setenv("REDIS_URL", TEST_URL)
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = "1-0"
        set_redis_client(mock_redis)

        assert publish("vision", "vision.person", {"camera": "test-camera"}) == "1-0"
        mock_redis.xadd.assert_called_once()

    def test_conftest_pins_redis_to_an_allowed_namespace(self):
        """The ambient REDIS_URL of any test run must satisfy the guard.

        Unconditional: the version this replaces skipped its own assertion when
        REDIS_URL was unset, which is precisely the case that resolves to
        production db 0.
        """
        namespace = _namespace_from_url(os.environ["REDIS_URL"])
        assert namespace in _allowed_namespaces()
