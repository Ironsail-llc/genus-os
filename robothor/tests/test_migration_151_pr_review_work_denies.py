"""151: the review worker and the review report under the 146 pattern.

``pr_review_work`` runs reviews (Claude Code jobs, GitHub posts) and is run by
the pr-review-run workflow as ``service``. ``pr_review_status`` is read-only,
but it names the instance's pull requests, authors and review state, so it is
the operator's view: main's heartbeat (``service``) and the operator
(``owner``/``admin``) have it; other humans (``user``/``member``) do not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "151_pr_review_work_denies"
TOOLS = ("pr_review_work", "pr_review_status")


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
    def connect(**_kw):
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    return permissions.check_tool_permission(role, "tenant-under-test", tool)


@pytest.mark.parametrize("role", ["user", "member"])
@pytest.mark.parametrize("tool", TOOLS)
def test_humans_other_than_the_operator_are_denied(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool) is not None


@pytest.mark.parametrize("role", ["service", "owner", "admin"])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_workflow_the_heartbeat_and_the_operator_keep_them(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool) is None
