"""Every tenant_id table a migration creates must carry the tenant_isolation policy.

Migration 081 applied Row-Level Security to the tenant tables that existed when
it ran and to nothing after. Six tables created later (identity tables among
them) had no policy on the production database for months. Migration 120
re-applies the backstop; this test makes the next new table unable to reopen
the gap: a migration after the last backstop that creates a table with a
tenant_id column must apply ``tenant_isolation`` to that table in the same
file, or ship a new backstop.

Static on purpose: it reads the manifest, so it runs everywhere without a
database. The live-database half is the doctor check ``db.rls_coverage``.
"""

from __future__ import annotations

import re
from pathlib import Path

from robothor.db import migrate

_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_ADD_TENANT = re.compile(
    r"ALTER\s+TABLE\s+(?:public\.)?(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?tenant_id\b",
    re.IGNORECASE,
)
# The generic loop that policies every tenant_id table in pg_tables.
_BACKSTOP = re.compile(r"FROM\s+pg_tables\s+pt.*?tenant_isolation", re.IGNORECASE | re.DOTALL)


def _tenant_tables(sql: str) -> set[str]:
    tables = {name for name, body in _CREATE.findall(sql) if re.search(r"\btenant_id\b", body)}
    tables |= set(_ADD_TENANT.findall(sql))
    return {t.lower() for t in tables}


def _policies_inline(sql: str, table: str) -> bool:
    return (
        re.search(rf"tenant_isolation\s+ON\s+(?:public\.)?{re.escape(table)}\b", sql, re.IGNORECASE)
        is not None
    )


def _ordered_migrations() -> list[tuple[str, str]]:
    return [(Path(path).name, Path(path).read_text()) for _src, path in migrate._manifest_paths()]


def test_the_backstop_migration_ships_and_is_the_generic_loop() -> None:
    names = [name for name, _ in _ordered_migrations()]
    assert "120_tenant_rls_cover_new_tables.sql" in names
    sql = dict(_ordered_migrations())["120_tenant_rls_cover_new_tables.sql"]
    assert _BACKSTOP.search(sql)
    assert "FORCE ROW LEVEL SECURITY" in sql


def test_every_tenant_table_created_after_the_last_backstop_is_policied_inline() -> None:
    migrations = _ordered_migrations()
    last_backstop = max(i for i, (_n, sql) in enumerate(migrations) if _BACKSTOP.search(sql))
    bare = [
        f"{name}: {table}"
        for name, sql in migrations[last_backstop + 1 :]
        for table in _tenant_tables(sql)
        if not _policies_inline(sql, table)
    ]
    assert not bare, (
        "these migrations create a tenant_id table without applying tenant_isolation "
        "in the same file; add the policy inline or ship a new backstop migration: "
        + ", ".join(bare)
    )


def test_the_detector_sees_the_tables_the_gap_was_made_of() -> None:
    """The regexes must recognise the real migrations, or the guard is decoration."""
    by_name = dict(_ordered_migrations())
    assert "sso_binding_grants" in _tenant_tables(by_name["085_sso_binding_grants.sql"])
    assert "user_permissions" in _tenant_tables(by_name["086_user_permissions.sql"])
    assert "face_identities" in _tenant_tables(by_name["089_face_identities.sql"])
    assert not _policies_inline(by_name["085_sso_binding_grants.sql"], "sso_binding_grants")
