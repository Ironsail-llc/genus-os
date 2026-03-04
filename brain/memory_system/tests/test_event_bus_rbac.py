"""Tests for Event Bus RBAC enforcement.

Phase 3.4: Validates that subscribe() and publish() check agent capabilities
before allowing stream access.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.expanduser("~/robothor/brain/memory_system"))
import capabilities
import event_bus


@pytest.fixture(autouse=True)
def reset_state():
    """Reset event bus and capabilities between tests."""
    event_bus.reset_client()
    event_bus.EVENT_BUS_ENABLED = True
    capabilities.reset()
    old_redis_url = os.environ.pop("REDIS_URL", None)
    yield
    event_bus.reset_client()
    capabilities.reset()
    if old_redis_url is not None:
        os.environ["REDIS_URL"] = old_redis_url
    else:
        os.environ.pop("REDIS_URL", None)


# ─── Subscribe RBAC ───────────────────────────────────────────────


class TestSubscribeRBAC:
    def test_subscribe_allowed_agent(self):
        """Agent with stream read access can subscribe."""
        capabilities.load_capabilities()
        handler_called = []

        class FakeRedis:
            def ping(self):
                return True

            def xgroup_create(self, *a, **kw):
                pass

            def xreadgroup(self, *a, **kw):
                return None

            def xack(self, *a, **kw):
                pass

        event_bus.set_redis_client(FakeRedis())
        # email-classifier has streams_read: ["email"]
        # Should not return early — will enter the subscribe loop
        event_bus.subscribe(
            "email",
            "test-group",
            "test-consumer",
            handler=lambda e: handler_called.append(e),
            max_iterations=1,
            agent_id="email-classifier",
        )
        # If we get here, subscribe was allowed (it ran 1 iteration)

    def test_subscribe_denied_agent(self):
        """Agent without stream read access is blocked from subscribing."""
        capabilities.load_capabilities()
        handler_called = []

        class FakeRedis:
            def ping(self):
                return True

            def xgroup_create(self, *a, **kw):
                pass

            def xreadgroup(self, *a, **kw):
                handler_called.append("xreadgroup-called")
                return None

        event_bus.set_redis_client(FakeRedis())
        # vision-monitor has streams_read: ["vision"], NOT "email"
        event_bus.subscribe(
            "email",
            "test-group",
            "test-consumer",
            handler=lambda e: None,
            max_iterations=1,
            agent_id="vision-monitor",
        )
        # xreadgroup should NOT have been called
        assert handler_called == [], "xreadgroup was called despite denied access"

    def test_subscribe_no_agent_id_allowed(self):
        """No agent_id (backward compat) skips RBAC check."""
        capabilities.load_capabilities()

        class FakeRedis:
            def ping(self):
                return True

            def xgroup_create(self, *a, **kw):
                pass

            def xreadgroup(self, *a, **kw):
                return None

        event_bus.set_redis_client(FakeRedis())
        # No agent_id — should proceed without RBAC check
        event_bus.subscribe(
            "crm",
            "test-group",
            "test-consumer",
            handler=lambda e: None,
            max_iterations=1,
        )
        # If we get here, it was allowed

    def test_subscribe_unknown_agent_default_allow(self):
        """Unknown agent gets default_policy (allow) for subscribe."""
        capabilities.load_capabilities()

        class FakeRedis:
            def ping(self):
                return True

            def xgroup_create(self, *a, **kw):
                pass

            def xreadgroup(self, *a, **kw):
                return None

        event_bus.set_redis_client(FakeRedis())
        # Unknown agent — default policy is "allow"
        event_bus.subscribe(
            "email",
            "test-group",
            "test-consumer",
            handler=lambda e: None,
            max_iterations=1,
            agent_id="unknown-agent-xyz",
        )
        # If we get here, it was allowed

    def test_subscribe_supervisor_reads_all(self):
        """Supervisor can subscribe to any stream."""
        capabilities.load_capabilities()

        class FakeRedis:
            def ping(self):
                return True

            def xgroup_create(self, *a, **kw):
                pass

            def xreadgroup(self, *a, **kw):
                return None

        event_bus.set_redis_client(FakeRedis())
        for stream in ["email", "crm", "health", "agent", "calendar", "vision", "system"]:
            event_bus.subscribe(
                stream,
                "test-group",
                "test-consumer",
                handler=lambda e: None,
                max_iterations=1,
                agent_id="supervisor",
            )
            # All should succeed


# ─── Publish RBAC ─────────────────────────────────────────────────


class TestPublishRBAC:
    def test_publish_allowed_agent(self):
        """Agent with stream write access can publish."""
        capabilities.load_capabilities()

        xadd_calls = []

        class FakeRedis:
            def ping(self):
                return True

            def xadd(self, key, fields, **kw):
                xadd_calls.append(key)
                return "1-0"

        event_bus.set_redis_client(FakeRedis())
        # crm-steward has streams_write: ["crm"]
        result = event_bus.publish(
            "crm",
            "crm.test",
            {"key": "value"},
            source="test",
            agent_id="crm-steward",
        )
        assert result == "1-0"
        assert len(xadd_calls) == 1

    def test_publish_denied_agent(self):
        """Agent without stream write access is blocked from publishing."""
        capabilities.load_capabilities()

        xadd_calls = []

        class FakeRedis:
            def ping(self):
                return True

            def xadd(self, key, fields, **kw):
                xadd_calls.append(key)
                return "1-0"

        event_bus.set_redis_client(FakeRedis())
        # email-classifier has streams_write: [] — cannot write to anything
        result = event_bus.publish(
            "email",
            "email.test",
            {"key": "value"},
            source="test",
            agent_id="email-classifier",
        )
        assert result is None
        assert xadd_calls == [], "xadd was called despite denied write access"

    def test_publish_no_agent_id_allowed(self):
        """No agent_id (backward compat) skips RBAC check."""
        capabilities.load_capabilities()

        class FakeRedis:
            def ping(self):
                return True

            def xadd(self, key, fields, **kw):
                return "1-0"

        event_bus.set_redis_client(FakeRedis())
        result = event_bus.publish(
            "email",
            "email.test",
            {"key": "value"},
            source="test",
        )
        assert result == "1-0"

    def test_publish_unknown_agent_default_allow(self):
        """Unknown agent gets default_policy (allow) for publish."""
        capabilities.load_capabilities()

        class FakeRedis:
            def ping(self):
                return True

            def xadd(self, key, fields, **kw):
                return "1-0"

        event_bus.set_redis_client(FakeRedis())
        result = event_bus.publish(
            "crm",
            "crm.test",
            {"key": "value"},
            source="test",
            agent_id="rogue-agent-xyz",
        )
        assert result == "1-0"
