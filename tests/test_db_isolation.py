"""The test suite must never touch a production database.

Two layers protect that invariant:

1. The root ``conftest.py`` pins ``ROBOTHOR_DB_NAME=robothor_test`` (setdefault),
   so a plain ``pytest`` run resolves the test database instead of the
   platform default ``robothor_memory``.
2. ``robothor.db.connection`` refuses to open a pool (or any direct fallback
   connection) from inside pytest unless the resolved database name ends with
   ``_test`` or is explicitly allowed via ``ROBOTHOR_TEST_DB_ALLOW`` (used by
   the release gate, which legitimately runs against ``robothor_release_gate``).

These tests pin both layers. If either regresses, local test runs silently
write chat sessions, audit rows, and guardrail escalations into production
again — see the 2026-08-20 pollution inventory (9,174 chat_messages, 92
phantom operator escalations, 1,554 audit rows).
"""

from __future__ import annotations

import os

import pytest

import robothor.config as config_mod
import robothor.db.connection as conn_mod
from robothor.db.connection import assert_test_database


@pytest.fixture
def fresh_config():
    """Reset the config singleton around a test so env changes are observed."""
    config_mod.reset_config()
    yield
    config_mod.reset_config()


class TestResolvedConfig:
    def test_pytest_resolves_a_test_database(self, fresh_config):
        """Under pytest the lazily-resolved DB config must name a test DB."""
        name = config_mod.get_config().db.name
        allowed = os.environ.get("ROBOTHOR_TEST_DB_ALLOW", "")
        assert name.endswith("_test") or name == allowed, (
            f"pytest resolved database {name!r} — the root conftest.py must pin "
            "ROBOTHOR_DB_NAME to a *_test database so tests cannot write to production"
        )


class TestAssertTestDatabase:
    """Unit tests for the guard itself."""

    def test_allows_test_suffixed_name_under_pytest(self):
        assert_test_database("robothor_test")  # must not raise

    def test_refuses_production_name_under_pytest(self):
        with pytest.raises(RuntimeError, match="Refusing"):
            assert_test_database("robothor_memory")

    def test_explicit_allow_via_env(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TEST_DB_ALLOW", "robothor_release_gate")
        assert_test_database("robothor_release_gate")  # must not raise

    def test_allow_env_does_not_open_other_names(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TEST_DB_ALLOW", "robothor_release_gate")
        with pytest.raises(RuntimeError, match="Refusing"):
            assert_test_database("robothor_memory")

    def test_noop_outside_pytest(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert_test_database("robothor_memory")  # production is fine outside tests


class TestConnectionGuards:
    def test_get_pool_refuses_non_test_database(self, monkeypatch, fresh_config):
        """A misconfigured env inside pytest must hard-fail, not connect to prod."""
        monkeypatch.setenv("ROBOTHOR_DB_NAME", "robothor_memory")
        monkeypatch.delenv("ROBOTHOR_TEST_DB_ALLOW", raising=False)
        config_mod.reset_config()
        monkeypatch.setattr(conn_mod, "_pool", None)
        with pytest.raises(RuntimeError, match="Refusing"):
            conn_mod.get_pool()

    def test_audit_logger_direct_dsn_fallback_is_guarded(self, monkeypatch, fresh_config):
        """The audit logger's pool-unavailable fallback must not reach prod either.

        ``robothor.audit.logger._get_connection`` catches *any* pool failure and
        falls back to ``psycopg2.connect(cfg.db.dsn)`` — so the guard must be
        applied on that path too, or the pool guard would be routed around.
        """
        from robothor.audit import logger as audit_logger

        monkeypatch.setenv("ROBOTHOR_DB_NAME", "robothor_memory")
        monkeypatch.delenv("ROBOTHOR_TEST_DB_ALLOW", raising=False)
        config_mod.reset_config()
        monkeypatch.setattr(conn_mod, "_pool", None)
        assert audit_logger._conn_factory is None, "test assumes no factory override"
        with pytest.raises(RuntimeError, match="Refusing"):
            audit_logger._get_connection()
