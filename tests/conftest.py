"""
Local conftest for tests/ — re-exports the integration fixtures defined in
``tests/conftest_integration.py`` so any test under tests/ that requests
``db_conn`` / ``db_cursor`` / ``mock_get_connection`` gets them automatically.

The integration fixtures themselves live in conftest_integration.py to keep
the file name greppable; this thin shim wires them into pytest's discovery.
"""

from tests.conftest_integration import (  # noqa: F401
    db_conn,
    db_cursor,
    db_dsn,
    mock_get_connection,
    redis_client,
    redis_url,
)
