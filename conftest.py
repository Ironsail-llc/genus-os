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
        scratch_db,
    )
except Exception:
    # ImportError when fixtures aren't on sys.path (bridge tests have their own rootdir).
    # OSError when an unrelated installed `tests` package shadows ours and pulls in torch
    # whose DLL fails to load on this host. In both cases the integration fixtures are
    # genuinely unavailable, which is the same outcome.
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


@pytest.fixture(autouse=True)
def _hermetic_env() -> object:
    """Snapshot os.environ around every test, and restore it afterwards.

    The suite must not depend on the machine it runs on, and one test must not be
    able to reconfigure the next.

    This is not hypothetical. ``robothor.cli.main()`` calls ``load_instance_env()``,
    which adopts the instance's systemd drop-in environment — correct and
    deliberate for a real CLI run (otherwise a shell reads every rollout-gated
    guardrail back as off/observe while the daemon enforces it). But it mutates the
    process-global ``os.environ``. So ``tests/test_setup.py::test_init_help``
    imported this box's live ``ROBOTHOR_SANDBOX_BINARY=podman`` and left it there,
    and ``test_sandbox.py::test_docker_exec`` — hundreds of tests later — asserted
    ``'podman' == 'docker'`` and failed. Nothing about either test had changed. The
    HOST had changed.

    CI never caught it, because CI has no drop-in. A suite whose result depends on
    the machine it runs on is not a suite; it is a coincidence.
    """
    import os

    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
