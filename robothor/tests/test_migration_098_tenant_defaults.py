"""098 retargets tenant_id column DEFAULTs off the first instance's tenant id.

Migrations 033/063/075 (crm) and 031 (infra) shipped ``DEFAULT
'robothor-primary'`` on tenant_id columns — the first instance's tenant baked
into every install (CLAUDE.md rule 1).  098 moves those DEFAULTs to
``'default'``, the tenant every install seeds in 008/001, EXCEPT the two
columns whose writers still rely on the column DEFAULT (retargeting those
would silently move new rows between tenants on the first instance):

- ``benchmark_results.tenant_id`` — robothor/engine/tools/handlers/benchmark.py
  inserts without tenant_id.
- ``memory_insights.tenant_id`` — robothor/memory/lifecycle.py inserts without
  tenant_id.

Pure file-parse tests (no DB), test_schema_drift style: the coverage assertion
is derived from the source migrations themselves, so a future migration that
reintroduces the literal — or a new writer that starts relying on a kept
DEFAULT — shows up as drift here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "crm" / "migrations" / "098_tenant_column_defaults.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"

SOURCE_MIGRATIONS = [
    REPO / "crm" / "migrations" / "033_memory_multi_tenancy.sql",
    REPO / "crm" / "migrations" / "063_benchmark_results.sql",
    REPO / "crm" / "migrations" / "075_operator_signals.sql",
    REPO / "infra" / "migrations" / "031_agent_reviews.sql",
]

RETARGETED = {
    "memory_facts",
    "memory_entities",
    "memory_relations",
    "agent_memory_blocks",
    "contact_identifiers",
    "ingested_items",
    "ingestion_watermarks",
    "message_reactions",
    "run_interventions",
    "agent_reviews",
}

# Writers still rely on the column DEFAULT for these — kept, documented in 098.
KEPT = {"benchmark_results", "memory_insights"}

_TABLE_RE = re.compile(
    r"(?:ALTER\s+TABLE|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([a-z_]+)",
    re.IGNORECASE,
)


def _tables_with_hardcoded_default() -> set[str]:
    """Tables whose tenant_id DEFAULT is the instance literal, per source SQL."""
    found: set[str] = set()
    for path in SOURCE_MIGRATIONS:
        for statement in path.read_text().split(";"):
            if "DEFAULT 'robothor-primary'" not in statement:
                continue
            match = _TABLE_RE.search(statement)
            assert match, f"unparseable statement in {path.name}: {statement[:120]!r}"
            found.add(match.group(1).lower())
    return found


def test_migration_file_exists():
    assert MIGRATION.exists(), "098_tenant_column_defaults.sql must exist"


def test_registered_in_manifest():
    entries = [
        line.strip()
        for line in MANIFEST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "crm/098_tenant_column_defaults.sql" in entries


def test_source_migration_inventory_matches():
    """The source-migration grep and the RETARGETED/KEPT split must agree."""
    assert _tables_with_hardcoded_default() == RETARGETED | KEPT


def test_retargets_exactly_the_intended_columns():
    sql = MIGRATION.read_text()
    altered = {
        table.lower()
        for table in re.findall(
            r"ALTER\s+TABLE\s+([a-z_]+)\s+ALTER\s+COLUMN\s+tenant_id\s+"
            r"SET\s+DEFAULT\s+'default'",
            sql,
            re.IGNORECASE,
        )
    }
    assert altered == RETARGETED


def test_kept_columns_are_documented_but_not_altered():
    sql = MIGRATION.read_text()
    for table in KEPT:
        assert table in sql, f"{table} must be documented as deliberately kept"
        assert not re.search(
            rf"ALTER\s+TABLE\s+{table}\b",
            sql,
            re.IGNORECASE,
        ), f"{table} relies on its column DEFAULT and must not be altered"


def test_sets_no_new_instance_default():
    sql = MIGRATION.read_text()
    assert not re.search(r"SET\s+DEFAULT\s+'robothor-primary'", sql, re.IGNORECASE), (
        "098 must never (re)introduce the instance tenant as a DEFAULT"
    )
