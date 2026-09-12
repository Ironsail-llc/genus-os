"""PostgreSQL: reachable, migrated, and holding the rows a fresh install needs.

``db.rbac_service_role`` is here because of a specific, expensive failure. On a
clean containerised instance stood up for the WildClawBench harness, every
scheduled agent was denied every tool: ``check_tool_permission`` fails closed
when a role has no rules, migration 037 seeds six roles and not ``service``,
and the production box had the row only because someone inserted it by hand in
July. Nothing could see that from the outside -- the engine was up, ``/ready``
was green, and the agents simply did nothing. Migration 107 seeds it; this
check is what tells an operator whether their database actually has it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, FixResult, Result, fail, ok

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS", "SERVICE_ROLE_MIGRATION"]

#: The migration that seeds the ``service`` role. The repair below EXECUTES
#: this file rather than carrying a copy of its INSERT: a hand-copied statement
#: beside the migration it mirrors is the drift this project has paid for
#: repeatedly, and here the two disagreeing would mean the doctor "fixing" a
#: database into a state no migration produces.
SERVICE_ROLE_MIGRATION = "107_seed_service_role.sql"


def _migration_sql(name: str) -> str:
    """Read a migration's SQL out of the canonical manifest.

    Through ``_manifest_paths`` rather than a path built here, because the
    manifest is what decides whether a migration is the bundled copy inside the
    wheel or the development one in the checkout. A path assembled by hand
    would read the repo's file on a box running the package.
    """
    from robothor.db import migrate

    for _source, path in migrate._manifest_paths():
        if Path(path).name == name:
            return Path(path).read_text()
    raise FileNotFoundError(f"{name} is not in the canonical migration manifest")


async def _connect(ctx: DoctorContext) -> Result:
    """The platform can open a database connection.

    Everything else in Genus OS -- memory, the CRM, the run ledger, the auth
    accounts -- is in PostgreSQL, so a failure here is the whole instance. The
    detail carries the exception TYPE and the database name, never the DSN: a
    psycopg2 error message contains the connection string, and a connection
    string contains a password.
    """

    def _probe() -> str:
        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return ctx.settings.database.name

    try:
        name = await ctx.run_blocking(_probe)
    except Exception as exc:  # noqa: BLE001 - unreachable is a result
        return fail(f"cannot connect: {type(exc).__name__}")
    return ok(f"connected to {name}")


def _migration_rows() -> list[dict[str, Any]]:
    from robothor.db import migrate

    return migrate.status()


async def _migrations(ctx: DoctorContext) -> Result:
    """The canonical migration ledger is complete and undrifted.

    ``pending`` means the schema is older than the code that is running against
    it -- the state in which a query fails on a column that exists in the repo
    and not in the database. ``DRIFT`` means an applied migration's file has
    changed since it ran, and ``MISSING`` that the ledger records one this
    install does not ship: neither can be repaired by applying anything, and
    both need a human. Only pending is fixable, and only with ``--fix``.
    """
    try:
        rows = await ctx.run_blocking(_migration_rows)
    except Exception as exc:  # noqa: BLE001 - a ledger that will not read is a failure
        return fail(f"cannot read the migration ledger: {type(exc).__name__}")

    pending = [row["migration_id"] for row in rows if row["status"] == "pending"]
    broken = [
        f"{row['migration_id']} ({row['status']})"
        for row in rows
        if row["status"] in {"DRIFT", "MISSING"}
    ]
    if broken:
        return fail(
            f"{len(broken)} migration(s) the ledger and this checkout disagree about: "
            + ", ".join(broken[:5])
            + " — this cannot be repaired by applying migrations; see 'genus migrate --status'"
        )
    if pending:
        return fail(
            f"{len(pending)} migration(s) pending, first {pending[0]} — "
            "run 'genus migrate' (or 'genus doctor --fix')",
            fixable=True,
        )
    return ok(f"{len(rows)} migration(s) applied, no drift")


async def _apply_migrations(ctx: DoctorContext) -> FixResult:
    """Apply the pending migrations. Idempotent: the migrator takes its own
    advisory lock and skips anything already recorded."""

    def _apply() -> list[str]:
        from robothor.db import migrate

        return migrate.apply()

    applied = await ctx.run_blocking(_apply)
    return FixResult(changed=bool(applied), detail=f"applied {len(applied)} migration(s)")


async def _service_role(ctx: DoctorContext) -> Result:
    """The ``service`` role has permission rules, so unattended runs can act.

    Every system-triggered run -- cron, hook, workflow, sub-agent -- is gated
    by the ``service`` role, and ``check_tool_permission`` DENIES a role that
    has no rules at all. A fresh install with RBAC in enforce and no seeded
    row therefore refuses every tool to every scheduled agent, silently: the
    engine is up, the agents run, and nothing they try is allowed. Repairable
    with ``--fix``, which executes migration 107.
    """

    def _probe() -> int:
        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM role_permissions WHERE role = 'service'")
            row = cursor.fetchone()
        return int(row[0] if not isinstance(row, dict) else next(iter(row.values())))

    try:
        count = await ctx.run_blocking(_probe)
    except Exception as exc:  # noqa: BLE001 - no table is itself the answer
        return fail(
            f"cannot read role_permissions: {type(exc).__name__} — the RBAC schema may not "
            "be migrated yet"
        )
    if count == 0:
        return fail(
            "the 'service' role has no permission rules, so every scheduled agent is "
            f"denied every tool — apply {SERVICE_ROLE_MIGRATION} "
            "(or 'genus doctor --fix')",
            fixable=True,
        )
    return ok(f"the 'service' role has {count} permission rule(s)")


async def _seed_service_role(ctx: DoctorContext) -> FixResult:
    """Run migration 107's own SQL. Idempotent -- it is an
    ``INSERT ... ON CONFLICT DO NOTHING``."""

    def _seed() -> None:
        sql = _migration_sql(SERVICE_ROLE_MIGRATION)
        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            conn.commit()

    await ctx.run_blocking(_seed)
    return FixResult(changed=True, detail=f"executed {SERVICE_ROLE_MIGRATION}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="db.connect",
        title="PostgreSQL is reachable",
        category="database",
        severity="required",
        run=_connect,
    ),
    Check(
        id="db.migrations",
        title="Migrations are applied and undrifted",
        category="database",
        severity="required",
        run=_migrations,
        fix=_apply_migrations,
    ),
    Check(
        id="db.rbac_service_role",
        title="The 'service' role is seeded",
        category="database",
        severity="required",
        run=_service_role,
        fix=_seed_service_role,
    ),
)
