"""Tests for robothor.audit.logger — uses mocked DB connections."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from robothor.audit.logger import (
    log_crm_mutation,
    log_event,
    log_telemetry,
    query_log,
    query_telemetry,
    reset_connection_factory,
    set_connection_factory,
    stats,
)


@pytest.fixture(autouse=True)
def clean_factory():
    """Reset connection factory between tests."""
    reset_connection_factory()
    yield
    reset_connection_factory()


def _mock_conn(fetchone_return=None, fetchall_return=None):
    """Create a mock connection with cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    if fetchone_return is not None:
        cursor.fetchone.return_value = fetchone_return
    if fetchall_return is not None:
        cursor.fetchall.return_value = fetchall_return
    return conn, cursor


class TestLogEvent:
    def test_successful_log(self):
        ts = datetime(2026, 2, 22, 12, 0, 0)
        conn, cursor = _mock_conn(fetchone_return=(42, ts))
        set_connection_factory(lambda: conn)

        result = log_event("crm.create", "Created person", actor="crm-steward")
        assert result is not None
        assert result["id"] == 42
        assert result["timestamp"] == ts.isoformat()
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()

    def test_log_with_all_fields(self):
        ts = datetime(2026, 2, 22, 12, 0, 0)
        conn, cursor = _mock_conn(fetchone_return=(1, ts))
        set_connection_factory(lambda: conn)

        result = log_event(
            "agent.action",
            "Searched memory",
            category="agent",
            actor="supervisor",
            session_key="sess-123",
            details={"query": "meetings"},
            source_channel="telegram",
            target="memory:facts",
            status="ok",
        )
        assert result is not None
        assert result["id"] == 1

    def test_log_failure_returns_none(self):
        conn = MagicMock()
        conn.cursor.side_effect = Exception("DB down")
        set_connection_factory(lambda: conn)

        result = log_event("test", "test action")
        assert result is None

    def test_log_never_raises(self):
        """Audit must never break callers."""
        set_connection_factory(lambda: (_ for _ in ()).throw(Exception("connection failed")))
        result = log_event("test", "test action")
        assert result is None


class TestLogCrmMutation:
    def test_create_person(self):
        ts = datetime(2026, 2, 22, 12, 0, 0)
        conn, cursor = _mock_conn(fetchone_return=(1, ts))
        set_connection_factory(lambda: conn)

        result = log_crm_mutation("create", "person", "uuid-123", actor="crm-steward")
        assert result is not None

        # Verify the SQL params
        call_args = cursor.execute.call_args[0]
        params = call_args[1]
        assert params[0] == "crm.create"  # event_type
        assert params[1] == "crm"  # category
        assert params[2] == "crm-steward"  # actor
        assert "create person uuid-123" in params[3]  # action
        assert params[8] is None  # session_key

    def test_merge_operation(self):
        ts = datetime(2026, 2, 22, 12, 0, 0)
        conn, cursor = _mock_conn(fetchone_return=(2, ts))
        set_connection_factory(lambda: conn)

        result = log_crm_mutation("merge", "company", "uuid-456")
        assert result is not None

    def test_no_entity_id(self):
        ts = datetime(2026, 2, 22, 12, 0, 0)
        conn, cursor = _mock_conn(fetchone_return=(3, ts))
        set_connection_factory(lambda: conn)

        result = log_crm_mutation("delete", "note", None)
        assert result is not None
        call_args = cursor.execute.call_args[0]
        params = call_args[1]
        assert params[6] == "note"  # target (no ID)


class TestQueryLog:
    def test_basic_query(self):
        rows = [
            (1, datetime(2026, 2, 22), "crm.create", "crm", "robot", "created", None, None, "p:1", "ok", None),
            (2, datetime(2026, 2, 21), "crm.update", "crm", "robot", "updated", None, None, "p:2", "ok", None),
        ]
        conn, cursor = _mock_conn(fetchall_return=rows)
        set_connection_factory(lambda: conn)

        results = query_log(limit=10)
        assert len(results) == 2
        assert results[0]["id"] == 1
        assert results[0]["event_type"] == "crm.create"

    def test_filtered_query(self):
        conn, cursor = _mock_conn(fetchall_return=[])
        set_connection_factory(lambda: conn)

        results = query_log(event_type="crm.create", actor="steward", limit=5)
        assert results == []

        # Verify filters were applied
        sql = cursor.execute.call_args[0][0]
        assert "event_type = %s" in sql
        assert "actor = %s" in sql

    def test_query_failure_returns_empty(self):
        set_connection_factory(lambda: (_ for _ in ()).throw(Exception("DB error")))
        results = query_log()
        assert results == []


class TestStats:
    def test_basic_stats(self):
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = (100, 5, datetime(2026, 1, 1), datetime(2026, 2, 22))
        cursor.fetchall.return_value = [("crm.create", 50), ("crm.update", 30)]
        set_connection_factory(lambda: conn)

        result = stats()
        assert result["total_events"] == 100
        assert result["unique_event_types"] == 5
        assert result["by_type"]["crm.create"] == 50

    def test_stats_failure(self):
        set_connection_factory(lambda: (_ for _ in ()).throw(Exception("DB error")))
        result = stats()
        assert result["total_events"] == 0
        assert "error" in result


class TestTelemetry:
    def test_log_telemetry(self):
        conn, cursor = _mock_conn()
        set_connection_factory(lambda: conn)

        result = log_telemetry("bridge", "request_count", 42.0, unit="count")
        assert result is True
        conn.commit.assert_called_once()

    def test_log_telemetry_with_details(self):
        conn, cursor = _mock_conn()
        set_connection_factory(lambda: conn)

        result = log_telemetry("vision", "detect_time", 0.5, unit="seconds", details={"model": "yolo"})
        assert result is True

    def test_telemetry_failure(self):
        set_connection_factory(lambda: (_ for _ in ()).throw(Exception("DB error")))
        result = log_telemetry("test", "metric", 1.0)
        assert result is False

    def test_query_telemetry(self):
        rows = [
            (1, datetime(2026, 2, 22), "bridge", "rps", 42.0, "req/s", None),
        ]
        conn, cursor = _mock_conn(fetchall_return=rows)
        set_connection_factory(lambda: conn)

        results = query_telemetry(service="bridge")
        assert len(results) == 1
        assert results[0]["metric"] == "rps"
        assert results[0]["value"] == 42.0

    def test_query_telemetry_failure(self):
        set_connection_factory(lambda: (_ for _ in ()).throw(Exception("DB error")))
        results = query_telemetry()
        assert results == []
