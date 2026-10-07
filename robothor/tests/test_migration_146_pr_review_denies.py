"""146: who may post reviews, drive Claude Code and run the pr_review_* tools.

Every unattended run dispatches as ``service`` (cron, workflow steps, a
workflow's agent step, sub-agents), so the pr-review workflows and main's
delegated coding need ``claude_code_*`` and ``pr_review_*`` under that role;
only the ``github_*`` review-write tools are denied to it, because
``pr_review_finalize`` calls their handlers directly. Humans other than the
operator (``user``, ``member``) are denied all nine; ``owner``/``admin`` keep
their catch-all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "146_pr_review_tool_denies"

REVIEW_WRITE = ("github_create_review", "github_reply_review_comment", "github_resolve_threads")
CODING = ("claude_code_start", "claude_code_followup", "claude_code_cancel")
PR_REVIEW = ("pr_review_intake", "pr_review_prepare", "pr_review_finalize")
ALL_NINE = REVIEW_WRITE + CODING + PR_REVIEW


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


def _decision(scratch_db, monkeypatch, role: str, tool: str, through: str) -> str | None:
    import contextlib

    import psycopg2

    from robothor.engine import permissions

    _, dsn = scratch_db(through=through)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    return permissions.check_tool_permission(role, "tenant-under-test", tool)


@pytest.mark.parametrize("role", ["user", "member"])
@pytest.mark.parametrize("tool", ALL_NINE)
def test_humans_other_than_the_operator_are_denied(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool, MIGRATION) is not None


@pytest.mark.parametrize("tool", REVIEW_WRITE)
def test_unattended_runs_cannot_post_reviews_directly(scratch_db, monkeypatch, tool):
    assert _decision(scratch_db, monkeypatch, "service", tool, MIGRATION) is not None


@pytest.mark.parametrize("tool", CODING + PR_REVIEW + ("claude_code_wait",))
def test_the_review_workflow_and_main_keep_what_they_run(scratch_db, monkeypatch, tool):
    # The pr-review workflows and main's heartbeat dispatch as 'service'.
    assert _decision(scratch_db, monkeypatch, "service", tool, MIGRATION) is None


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("tool", ALL_NINE)
def test_the_operator_keeps_everything(scratch_db, monkeypatch, role, tool):
    assert _decision(scratch_db, monkeypatch, role, tool, MIGRATION) is None
