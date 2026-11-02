"""Connection-pool acquisition and cleanup tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robothor.db import connection


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
