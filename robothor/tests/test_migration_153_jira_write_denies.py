"""153: who may write to Jira as the instance's account.

Humans other than the operator (``user``, ``member``) are denied the four
Jira write tools, as 146 denies them the GitHub review writes. ``service``
keeps its catch-all: an unattended agent that names the tools in its manifest
dispatches as ``service``, and the opt-in gate plus the runner's
``tools_allowed`` guard decide which agents those are. ``owner``/``admin``
keep everything. The reads stay open to everyone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "153_jira_write_tool_denies"

WRITES = ("jira_create_issue", "jira_add_comment", "jira_transition_issue", "jira_update_issue")


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


def test_the_migration_names_exactly_the_write_tools():
    from robothor.engine.tools.constants import JIRA_WRITE_TOOLS

    assert set(WRITES) == JIRA_WRITE_TOOLS


def _decision(scratch_db, monkeypatch, role: str, tool: str) -> str | None:
    import contextlib

    import psycopg2

    from robothor.engine import permissions

    _, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect(**_kw):
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    return permissions.check_tool_permission(role, "tenant-under-test", tool)


@pytest.mark.parametrize("role", ["user", "member"])
@pytest.mark.parametrize("tool", WRITES)
def test_humans_other_than_the_operator_are_denied(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool) is not None


@pytest.mark.parametrize("role", ["owner", "admin", "service"])
@pytest.mark.parametrize("tool", WRITES)
def test_the_operator_and_opted_in_unattended_agents_keep_them(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool) is None


@pytest.mark.parametrize("tool", ["jira_search", "jira_get_issue"])
def test_the_reads_stay_open_to_users(scratch_db, monkeypatch, tool):
    assert _decision(scratch_db, monkeypatch, "user", tool) is None
