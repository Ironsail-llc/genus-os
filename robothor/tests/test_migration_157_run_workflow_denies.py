"""157: who may start a workflow from a chat.

Humans other than the operator (``user``, ``member``) are denied
``run_workflow``, as the HTTP route it mirrors needs ``engine:control``.
``service`` keeps its catch-all: an unattended agent that names the tool in
its manifest dispatches as ``service``, and the opt-in gate plus the runner's
``tools_allowed`` guard decide which agents those are. ``owner``/``admin``
keep everything. The two read tools stay open to everyone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "157_run_workflow_tool_denies"


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


def test_the_migration_denies_run_workflow_to_user_and_member():
    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    assert "'run_workflow'" in sql
    assert "'user'" in sql and "'member'" in sql
    assert "'deny'" in sql
    assert "'__default__'" in sql


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


@pytest.mark.integration
@pytest.mark.parametrize("role", ["user", "member"])
def test_humans_other_than_the_operator_are_denied(scratch_db, monkeypatch, role):
    assert _decision(scratch_db, monkeypatch, role, "run_workflow") is not None


@pytest.mark.integration
@pytest.mark.parametrize("role", ["owner", "admin", "service"])
def test_the_operator_and_opted_in_unattended_agents_keep_it(scratch_db, monkeypatch, role):
    assert _decision(scratch_db, monkeypatch, role, "run_workflow") is None


@pytest.mark.integration
@pytest.mark.parametrize("tool", ["list_workflows", "workflow_runs"])
def test_the_reads_stay_open_to_users(scratch_db, monkeypatch, tool):
    assert _decision(scratch_db, monkeypatch, "user", tool) is None
