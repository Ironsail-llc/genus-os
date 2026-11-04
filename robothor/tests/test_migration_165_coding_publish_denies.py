"""165: who may clone a repository for coding and open a pull request.

``user`` and ``member`` -- humans other than the operator -- are denied both
``claude_code_repo_prepare`` and ``claude_code_publish``. ``service`` (every unattended
run) is denied ``claude_code_publish``: a pull request leaves the host, and only a
person with the owner's or an admin's role opens one. ``owner`` and ``admin``
keep their catch-all allow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "165_coding_publish_denies"


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


def test_the_migration_names_both_tools_and_the_three_roles():
    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    for token in (
        "'claude_code_publish'",
        "'claude_code_repo_prepare'",
        "'user'",
        "'member'",
        "'service'",
    ):
        assert token in sql, token
    assert "'deny'" in sql and "'__default__'" in sql


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


@pytest.mark.integration
@pytest.mark.parametrize("role", ["user", "member", "service"])
def test_publish_is_denied_below_the_operator(scratch_db, monkeypatch, role):
    assert _decision(scratch_db, monkeypatch, role, "claude_code_publish") is not None


@pytest.mark.integration
@pytest.mark.parametrize("role", ["user", "member"])
def test_prepare_is_denied_to_other_humans(scratch_db, monkeypatch, role):
    assert _decision(scratch_db, monkeypatch, role, "claude_code_repo_prepare") is not None


@pytest.mark.integration
def test_unattended_runs_may_still_prepare(scratch_db, monkeypatch):
    assert _decision(scratch_db, monkeypatch, "service", "claude_code_repo_prepare") is None


@pytest.mark.integration
@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("tool", ["claude_code_publish", "claude_code_repo_prepare"])
def test_the_operator_keeps_both(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool) is None
