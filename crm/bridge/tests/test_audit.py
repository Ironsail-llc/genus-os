"""
Tests for the Robothor audit logging system.

Tests cover:
- audit.log_event persistence and field handling
- audit.log_crm_mutation convenience wrapper
- audit.log_telemetry writes to telemetry table
- audit.query_log filtering
- audit.query_telemetry filtering
- audit.stats accuracy
- CRM DAL mutation logging (integration)
- Bridge audit query API endpoints
- Error resilience (audit never breaks callers)
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport
from unittest.mock import AsyncMock

# Add paths
BRIDGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, "/home/philip/clawd/memory_system")

import audit
import bridge_service
from bridge_service import app

# Prefix for test data isolation
TEST_PREFIX = f"__audit_test_{uuid.uuid4().hex[:6]}__"


@pytest.fixture(autouse=True)
def use_real_dsn():
    """Ensure audit module uses real database for integration tests."""
    audit.set_dsn("dbname=robothor_memory user=philip host=/var/run/postgresql")
    yield


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for bridge API testing."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    bridge_service.http_client = mock_http
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    bridge_service.http_client = None


# ─── audit.log_event Tests ─────────────────────────────────────────────


class TestLogEvent:
    def test_basic_event(self):
        result = audit.log_event(
            "test.basic", f"{TEST_PREFIX} basic event",
            category="test",
        )
        assert result is not None
        assert "id" in result
        assert "timestamp" in result

    def test_all_fields(self):
        result = audit.log_event(
            "test.full", f"{TEST_PREFIX} full event",
            category="test",
            actor="test-agent",
            session_key="test-session-123",
            details={"key": "value", "nested": {"a": 1}},
            source_channel="test",
            target="person:test-uuid",
            status="ok",
        )
        assert result is not None

        # Verify persisted correctly via query
        rows = audit.query_log(event_type="test.full", limit=1)
        assert len(rows) >= 1
        row = rows[0]
        assert row["actor"] == "test-agent"
        assert row["session_key"] == "test-session-123"
        assert row["details"]["key"] == "value"
        assert row["source_channel"] == "test"
        assert row["target"] == "person:test-uuid"

    def test_null_details(self):
        result = audit.log_event(
            "test.null_details", f"{TEST_PREFIX} null details",
        )
        assert result is not None

    def test_error_status(self):
        result = audit.log_event(
            "test.error", f"{TEST_PREFIX} error event",
            status="error",
            details={"error": "something went wrong"},
        )
        assert result is not None

    def test_default_actor(self):
        result = audit.log_event("test.actor", f"{TEST_PREFIX} default actor")
        rows = audit.query_log(event_type="test.actor", limit=1)
        assert rows[0]["actor"] == "robothor"


# ─── audit.log_crm_mutation Tests ──────────────────────────────────────


class TestLogCrmMutation:
    def test_create_mutation(self):
        test_id = str(uuid.uuid4())
        result = audit.log_crm_mutation(
            "create", "person", test_id,
            details={"first_name": "Test", "last_name": "User"},
        )
        assert result is not None

        rows = audit.query_log(event_type="crm.create", target=test_id, limit=1)
        assert len(rows) >= 1
        assert rows[0]["category"] == "crm"
        assert f"person:{test_id}" in rows[0]["target"]

    def test_merge_mutation(self):
        keeper_id = str(uuid.uuid4())
        result = audit.log_crm_mutation(
            "merge", "person", keeper_id,
            details={"loser_id": "loser-123"},
        )
        assert result is not None
        rows = audit.query_log(event_type="crm.merge", limit=1)
        assert len(rows) >= 1

    def test_error_mutation(self):
        result = audit.log_crm_mutation(
            "create", "person", None,
            details={"error": "validation failed"},
            status="error",
        )
        assert result is not None


# ─── audit.log_telemetry Tests ─────────────────────────────────────────


class TestLogTelemetry:
    def test_basic_telemetry(self):
        ok = audit.log_telemetry("test-service", "response_time_ms", 42.5, unit="ms")
        assert ok is True

    def test_telemetry_with_details(self):
        ok = audit.log_telemetry(
            "test-service", "http_status", 200,
            details={"endpoint": "/health"},
        )
        assert ok is True

    def test_telemetry_query(self):
        # Write a unique metric
        metric_name = f"test_metric_{uuid.uuid4().hex[:6]}"
        audit.log_telemetry("test-svc", metric_name, 99.9, unit="pct")

        rows = audit.query_telemetry(service="test-svc", metric=metric_name, limit=5)
        assert len(rows) >= 1
        assert rows[0]["value"] == 99.9
        assert rows[0]["unit"] == "pct"


# ─── audit.query_log Tests ─────────────────────────────────────────────


class TestQueryLog:
    def test_filter_by_event_type(self):
        audit.log_event("test.filter_type", f"{TEST_PREFIX} filter test")
        rows = audit.query_log(event_type="test.filter_type", limit=5)
        assert all(r["event_type"] == "test.filter_type" for r in rows)

    def test_filter_by_actor(self):
        audit.log_event("test.filter_actor", f"{TEST_PREFIX} actor test", actor="test-agent-xyz")
        rows = audit.query_log(actor="test-agent-xyz", limit=5)
        assert all(r["actor"] == "test-agent-xyz" for r in rows)

    def test_filter_by_target(self):
        target_id = str(uuid.uuid4())
        audit.log_event("test.filter_target", "target test", target=f"person:{target_id}")
        rows = audit.query_log(target=target_id, limit=5)
        assert len(rows) >= 1

    def test_limit(self):
        rows = audit.query_log(limit=3)
        assert len(rows) <= 3

    def test_filter_by_status(self):
        audit.log_event("test.status_filter", "status test", status="error")
        rows = audit.query_log(event_type="test.status_filter", status="error", limit=5)
        assert all(r["status"] == "error" for r in rows)


# ─── audit.stats Tests ─────────────────────────────────────────────────


class TestStats:
    def test_stats_returns_structure(self):
        s = audit.stats()
        assert "total_events" in s
        assert "unique_event_types" in s
        assert "by_type" in s
        assert s["total_events"] > 0


# ─── Error Resilience Tests ────────────────────────────────────────────


class TestErrorResilience:
    def test_log_event_bad_dsn_returns_none(self):
        """Audit must never raise — returns None on failure."""
        old_dsn = audit._pg_dsn
        audit.set_dsn("dbname=nonexistent_db_xyz user=nobody host=localhost")
        result = audit.log_event("test.fail", "should not raise")
        assert result is None
        audit.set_dsn(old_dsn)

    def test_query_log_bad_dsn_returns_empty(self):
        old_dsn = audit._pg_dsn
        audit.set_dsn("dbname=nonexistent_db_xyz user=nobody host=localhost")
        rows = audit.query_log(limit=5)
        assert rows == []
        audit.set_dsn(old_dsn)

    def test_telemetry_bad_dsn_returns_false(self):
        old_dsn = audit._pg_dsn
        audit.set_dsn("dbname=nonexistent_db_xyz user=nobody host=localhost")
        ok = audit.log_telemetry("svc", "metric", 1.0)
        assert ok is False
        audit.set_dsn(old_dsn)


# ─── Bridge Audit API Tests ───────────────────────────────────────────


class TestBridgeAuditAPI:
    @pytest.mark.asyncio
    async def test_audit_query_endpoint(self, client):
        # Insert a test event first
        audit.log_event("test.api", "api test event", category="test")

        resp = await client.get("/api/audit", params={"event_type": "test.api", "limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "count" in data

    @pytest.mark.asyncio
    async def test_audit_stats_endpoint(self, client):
        resp = await client.get("/api/audit/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_events" in data
        assert "by_type" in data

    @pytest.mark.asyncio
    async def test_telemetry_query_endpoint(self, client):
        # Insert test telemetry
        audit.log_telemetry("test-api-svc", "test_metric", 42.0)

        resp = await client.get("/api/telemetry", params={"service": "test-api-svc", "limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "count" in data


# ─── CRM DAL Audit Integration Tests ──────────────────────────────────


class TestCrmDalAudit:
    """Verify that CRM DAL write operations produce audit log entries."""

    @pytest.mark.integration
    def test_create_person_audited(self):
        import crm_dal

        person_id = crm_dal.create_person(
            f"{TEST_PREFIX}AuditTest", "Person",
            email=f"{TEST_PREFIX}@test.example.com",
        )
        assert person_id is not None

        # Check audit log for this creation
        rows = audit.query_log(event_type="crm.create", target=person_id, limit=5)
        assert len(rows) >= 1
        assert rows[0]["category"] == "crm"
        assert "AuditTest" in (rows[0]["details"] or {}).get("first_name", "")

        # Cleanup
        crm_dal.delete_person(person_id)

    @pytest.mark.integration
    def test_delete_person_audited(self):
        import crm_dal

        person_id = crm_dal.create_person(f"{TEST_PREFIX}DelTest", "Person")
        assert person_id is not None

        crm_dal.delete_person(person_id)

        rows = audit.query_log(event_type="crm.delete", target=person_id, limit=5)
        assert len(rows) >= 1

    @pytest.mark.integration
    def test_update_person_audited(self):
        import crm_dal

        person_id = crm_dal.create_person(f"{TEST_PREFIX}UpdTest", "Person")
        assert person_id is not None

        crm_dal.update_person(person_id, city="TestCity")

        rows = audit.query_log(event_type="crm.update", target=person_id, limit=5)
        assert len(rows) >= 1
        assert "city" in rows[0]["details"].get("fields", [])

        crm_dal.delete_person(person_id)


# ─── Cleanup ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="session")
def cleanup_test_audit_entries():
    """Clean up test audit entries after the session."""
    yield
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=robothor_memory user=philip host=/var/run/postgresql")
        cur = conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE action LIKE %s", (f"%{TEST_PREFIX}%",))
        cur.execute("DELETE FROM audit_log WHERE event_type LIKE 'test.%'")
        cur.execute("DELETE FROM telemetry WHERE service LIKE 'test%'")
        conn.commit()
        conn.close()
    except Exception:
        pass
