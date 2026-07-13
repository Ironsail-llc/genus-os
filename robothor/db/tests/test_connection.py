"""Connection-pool acquisition and cleanup tests."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from robothor.db import connection


def test_late_connection_is_returned_after_timeout(monkeypatch):
    pool = MagicMock()
    candidate = MagicMock()
    allow_acquire = threading.Event()
    returned = threading.Event()

    def delayed_getconn():
        assert allow_acquire.wait(timeout=1)
        return candidate

    def putconn(conn):
        assert conn is candidate
        returned.set()

    pool.getconn.side_effect = delayed_getconn
    pool.putconn.side_effect = putconn
    monkeypatch.setattr(connection, "get_pool", lambda: pool)
    monkeypatch.setattr(connection, "_POOL_GETCONN_TIMEOUT", 0.01)

    with pytest.raises(ConnectionError, match="pool exhausted"):
        with connection.get_connection():
            pytest.fail("timed-out acquisition must not yield")

    allow_acquire.set()
    assert returned.wait(timeout=1), "late connection was orphaned instead of returned"
    pool.putconn.assert_called_once_with(candidate)


def test_pool_acquisition_error_keeps_original_cause(monkeypatch):
    pool = MagicMock()
    cause = RuntimeError("pool closed")
    pool.getconn.side_effect = cause
    monkeypatch.setattr(connection, "get_pool", lambda: pool)

    with pytest.raises(ConnectionError, match="Failed to acquire") as raised:
        with connection.get_connection():
            pytest.fail("failed acquisition must not yield")

    assert raised.value.__cause__ is cause
    pool.putconn.assert_not_called()
