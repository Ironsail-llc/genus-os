"""152: ``config_get`` / ``config_set`` are the owner's (GEN-42).

The handlers refuse anyone but a verified owner identity; this is the RBAC
half. Every role but ``owner`` is denied both (``viewer`` already holds a
``*`` deny).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "152_config_tool_denies"
TOOLS = ("config_get", "config_set")


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


def _decision(scratch_db, monkeypatch, role: str, tool: str) -> str | None:
    import contextlib

    import psycopg2

    from robothor.engine import permissions

    _, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    return permissions.check_tool_permission(role, "tenant-under-test", tool)


@pytest.mark.parametrize("role", ["service", "user", "member", "admin", "viewer"])
@pytest.mark.parametrize("tool", TOOLS)
def test_every_role_but_the_owner_is_denied(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool) is not None


@pytest.mark.parametrize("tool", TOOLS)
def test_the_owner_keeps_them(scratch_db, monkeypatch, tool):
    assert _decision(scratch_db, monkeypatch, "owner", tool) is None
