"""The database category, including the fresh-install defect it exists for.

``db.rbac_service_role`` is the check that would have caught the WildClawBench
container in which every agent was denied every tool. Its repair executes
migration 107's own SQL rather than a copy of the INSERT, so the two cannot
drift -- and that is asserted here, because a hand-copied statement is exactly
the failure mode this project keeps paying for.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.doctor.checks import database as db_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx


def _check(check_id: str):
    return next(check for check in db_checks.CHECKS if check.id == check_id)


def _run(check_id: str, ctx):
    return asyncio.run(_check(check_id).run(ctx))


# ── db.connect ───────────────────────────────────────────────────────────────


def test_connect_passes_and_names_the_database() -> None:
    ctx = make_ctx(db_factory=fake_db([(1,)]))
    result = _run("db.connect", ctx)
    assert result.status == "pass"
    assert "robothor_memory" in result.detail


def test_connect_fails_without_leaking_the_dsn() -> None:
    """A psycopg2 error message carries the connection string, and a
    connection string carries a password."""
    boom = RuntimeError("could not connect: password=hunter2 host=db.internal")
    ctx = make_ctx(db_factory=fake_db(error=boom))
    result = _run("db.connect", ctx)
    assert result.status == "fail"
    assert "hunter2" not in result.detail
    assert "db.internal" not in result.detail
    assert "RuntimeError" in result.detail


# ── db.migrations ────────────────────────────────────────────────────────────


def _status_rows(monkeypatch, rows):
    monkeypatch.setattr(db_checks, "_migration_rows", lambda: rows)


def test_migrations_pass_when_everything_is_applied(monkeypatch) -> None:
    _status_rows(monkeypatch, [{"migration_id": "001", "status": "applied"}])
    result = _run("db.migrations", make_ctx())
    assert result.status == "pass"
    assert result.fixable is False


def test_pending_migrations_fail_and_say_they_are_repairable(monkeypatch) -> None:
    _status_rows(
        monkeypatch,
        [
            {"migration_id": "001", "status": "applied"},
            {"migration_id": "114", "status": "pending"},
        ],
    )
    result = _run("db.migrations", make_ctx())
    assert result.status == "fail"
    assert result.fixable is True
    assert "114" in result.detail
    assert "genus migrate" in result.detail


def test_drift_fails_and_is_explicitly_not_repairable(monkeypatch) -> None:
    """Applying migrations cannot fix a checksum that no longer matches, and a
    --fix that tried would write a schema no migration produces."""
    _status_rows(monkeypatch, [{"migration_id": "037", "status": "DRIFT"}])
    result = _run("db.migrations", make_ctx())
    assert result.status == "fail"
    assert result.fixable is False
    assert "037" in result.detail


def test_a_ledger_row_this_checkout_does_not_ship_fails(monkeypatch) -> None:
    _status_rows(monkeypatch, [{"migration_id": "999", "status": "MISSING"}])
    result = _run("db.migrations", make_ctx())
    assert result.status == "fail"
    assert result.fixable is False


def test_a_role_that_may_not_read_the_ledger_is_a_skip(monkeypatch) -> None:
    """A least-privilege deployment applies migrations as one account and runs
    the services as another. Failing here would leave `genus doctor` exiting 1
    forever on a correctly configured box, which is how an operator learns to
    ignore red output."""

    class DeniedError(RuntimeError):
        pgcode = "42501"

    def _raise():
        raise DeniedError("permission denied for schema public")

    monkeypatch.setattr(db_checks, "_migration_rows", _raise)
    result = _run("db.migrations", make_ctx())
    assert result.status == "skip"
    assert "genus migrate --status" in result.detail


def test_any_other_ledger_error_is_still_a_failure(monkeypatch) -> None:
    def _raise():
        raise RuntimeError("the manifest is missing")

    monkeypatch.setattr(db_checks, "_migration_rows", _raise)
    result = _run("db.migrations", make_ctx())
    assert result.status == "fail"
    assert "RuntimeError" in result.detail


def test_the_migration_fix_reruns_the_check_and_it_passes(monkeypatch) -> None:
    from robothor.doctor.runner import run_sync

    state = {"applied": False}

    def _rows():
        if state["applied"]:
            return [{"migration_id": "114", "status": "applied"}]
        return [{"migration_id": "114", "status": "pending"}]

    def _apply():
        state["applied"] = True
        return ["114"]

    monkeypatch.setattr(db_checks, "_migration_rows", _rows)
    monkeypatch.setattr("robothor.db.migrate.apply", _apply)

    report = run_sync(make_ctx(fix=True), checks=[_check("db.migrations")])
    assert state["applied"] is True
    assert report.results[0].status == "pass"
    assert report.exit_code == 0


def test_a_repair_the_database_user_may_not_perform_stays_a_failure(monkeypatch) -> None:
    from robothor.doctor.runner import run_sync

    _status_rows(monkeypatch, [{"migration_id": "114", "status": "pending"}])

    def _refuse():
        raise PermissionError("permission denied for table schema_migrations")

    monkeypatch.setattr("robothor.db.migrate.apply", _refuse)
    report = run_sync(make_ctx(fix=True), checks=[_check("db.migrations")])
    assert report.results[0].status == "fail"
    assert "PermissionError" in report.results[0].detail


# ── db.rbac_service_role ─────────────────────────────────────────────────────


def test_a_seeded_service_role_passes() -> None:
    ctx = make_ctx(db_factory=fake_db([(1,)]))
    result = _run("db.rbac_service_role", ctx)
    assert result.status == "pass"
    assert "1 permission rule" in result.detail


def test_an_unseeded_service_role_fails_and_explains_the_consequence() -> None:
    ctx = make_ctx(db_factory=fake_db([(0,)]))
    result = _run("db.rbac_service_role", ctx)
    assert result.status == "fail"
    assert result.fixable is True
    assert "denied every tool" in result.detail
    assert db_checks.SERVICE_ROLE_MIGRATION in result.detail


def test_a_missing_rbac_table_is_a_failure_not_a_crash() -> None:
    ctx = make_ctx(db_factory=fake_db([(0,)], error=RuntimeError('relation "role_permissions"')))
    result = _run("db.rbac_service_role", ctx)
    assert result.status == "fail"
    assert "RuntimeError" in result.detail


def test_the_service_role_fix_executes_the_migrations_own_sql(monkeypatch) -> None:
    """Not a copy of the INSERT. A statement hand-copied beside the migration
    it mirrors is how the two end up describing different databases."""
    from robothor.doctor.runner import run_sync

    state = {"seeded": 0}
    factory = fake_db([(0,), (1,)])

    seen: list[str] = []
    real = db_checks._migration_sql

    def _spy(name: str) -> str:
        state["seeded"] += 1
        sql = real(name)
        seen.append(sql)
        return sql

    monkeypatch.setattr(db_checks, "_migration_sql", _spy)
    report = run_sync(
        make_ctx(fix=True, db_factory=factory), checks=[_check("db.rbac_service_role")]
    )

    assert state["seeded"] == 1
    assert "role_permissions" in seen[0]
    assert "ON CONFLICT DO NOTHING" in seen[0]
    assert report.results[0].status == "pass"


def test_the_seed_migration_ships_with_this_install() -> None:
    sql = db_checks._migration_sql(db_checks.SERVICE_ROLE_MIGRATION)
    assert "INSERT INTO role_permissions" in sql


def test_an_unknown_migration_name_raises_rather_than_seeding_nothing() -> None:
    with pytest.raises(FileNotFoundError):
        db_checks._migration_sql("999_not_a_migration.sql")
