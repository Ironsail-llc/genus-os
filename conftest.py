"""
Root-level shared test fixtures.

Inherited by engine, health, and any other test suites that run from the
repo root.  Bridge tests run from their own rootdir and are unaffected.

Integration fixtures (db_conn, db_cursor, mock_get_connection) are re-exported
from tests/conftest_integration.py so any test marked @pytest.mark.integration
can request them by name without per-suite duplication.
"""

from __future__ import annotations

# Pin DEFAULT_TENANT before any robothor import — the value is captured by
# function-default kwargs at dal.py import time.
import os as _os

_os.environ["ROBOTHOR_DEFAULT_TENANT"] = "default"

import uuid  # noqa: E402

import pytest  # noqa: E402

# Bridge tests are run from crm/bridge/ as their own rootdir; the tests
# package isn't on their sys.path. Integration fixtures are optional there,
# so only import when the tests package is resolvable.
try:
    from tests.conftest_integration import (  # noqa: E402, F401 — pytest-discovered fixtures
        _install_session_patch,
        db_conn,
        db_cursor,
        db_dsn,
        mock_get_connection,
        redis_client,
        redis_url,
    )
except ImportError:
    pass


@pytest.fixture
def test_prefix():
    """Unique prefix for test isolation."""
    return f"test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def clean_env(monkeypatch):
    """Remove common env vars that leak between tests."""
    for key in [
        "ROBOTHOR_DB_HOST",
        "ROBOTHOR_DB_PORT",
        "ROBOTHOR_DB_NAME",
        "ROBOTHOR_DB_USER",
        "ROBOTHOR_DB_PASSWORD",
    ]:
        monkeypatch.delenv(key, raising=False)
