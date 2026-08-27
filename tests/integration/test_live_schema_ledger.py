"""The ledger must describe the database it claims to describe.

Everything guarding migrations before this was file-vs-file:

* ``test_schema_drift.py`` says so in its own first line -- "pure file I/O, no
  database". It compares Python enums to SQL text, both in git, both consistent.
* ``test_db_migrate.py`` uses fake connection objects and tmp_path fixtures.
* ``test_migration_packaging.py`` checks manifest <-> disk <-> wheel.
* CI runs ``migrate --check`` against a throwaway Postgres it just built from
  scratch, where all 111 apply cleanly and the answer is always yes.

So CI proves the CHAIN is internally consistent. Nothing had ever validated a
LONG-LIVED database against that chain, and on 2026-08-27 production had drifted
in both directions at once: 29 migrations unrecorded (26 of them actually
applied), while ``agent_runs.resume_attempts`` was recorded nowhere and applied
nowhere despite ``daemon.py:90,119`` referencing it. Migrations 071 and 085 had
silently gone missing once before.

The danger is asymmetric, which is why this test exists rather than a lint:
a bare ``robothor migrate`` on that ledger would have applied 29 migrations in
prefix order, including ``infra/035_drop_legacy_buddy_columns``, whose guard
passes and which drops 22 columns.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

pytestmark = pytest.mark.integration


def _conn():
    db = os.environ.get("ROBOTHOR_DB_NAME", "robothor_test")
    try:
        c = psycopg2.connect(dbname=db)
        c.set_session(readonly=True)
        return c
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no database available: {exc}")


#: Migrations deliberately NOT applied, each with a reason. An entry here is a
#: decision on the record, not a silent pass -- the whole point of this file is
#: that "unrecorded" and "deferred" must not look the same.
DEFERRED: dict[str, str] = {
    "035_drop_legacy_buddy_columns": (
        "Destructive: 27 DROP COLUMN statements across agent_buddy_stats and "
        "buddy_stats. Its own precondition guard PASSES on production data, so a "
        "bare `migrate` would execute it. It archives to migration_archive_035_"
        "buddy_rpg first, and robothor_test has already taken it -- but dropping "
        "22 columns of production history is a data decision for the operator, "
        "not a reconciliation step. Deferred 2026-08-27."
    ),
}


def _recorded(cur) -> dict[str, str]:
    cur.execute("SELECT to_regclass('public.schema_migrations_v2')")
    if cur.fetchone()[0] is None:
        pytest.skip("schema_migrations_v2 absent — not a migrated database")
    cur.execute("SELECT migration_id, checksum FROM schema_migrations_v2")
    return dict(cur.fetchall())


def test_every_manifest_migration_is_recorded():
    """Applied-but-unrecorded is the dangerous half.

    An unrecorded migration is 'pending' to the runner, so a bare `migrate`
    re-runs it. Most are idempotent; 035 is not.
    """
    from robothor.db import migrate as m

    conn = _conn()
    try:
        recorded = _recorded(conn.cursor())
        missing = [
            x.migration_id
            for x in m._discover()
            if x.migration_id not in recorded and x.migration_id not in DEFERRED
        ]
        assert not missing, (
            f"{len(missing)} manifest migration(s) are not in schema_migrations_v2: "
            f"{missing}. Each is 'pending' to the runner. Backfill the ones already "
            f"applied and apply the rest BY NAME — never a bare `migrate`, which "
            f"would run them all in prefix order."
        )
    finally:
        conn.close()


def test_no_recorded_migration_has_drifted():
    """A recorded checksum that no longer matches the file means the schema and
    the repo disagree about what was run."""
    from robothor.db import migrate as m

    conn = _conn()
    try:
        recorded = _recorded(conn.cursor())
        drifted = [
            x.migration_id
            for x in m._discover()
            if x.migration_id in recorded and recorded[x.migration_id] != x.checksum
        ]
        assert not drifted, f"checksum drift between file and ledger: {drifted}"
    finally:
        conn.close()


def test_the_ledger_has_no_rows_the_manifest_does_not_know():
    """The reverse direction: a recorded migration with no file.

    Legacy instance-local rows (the Delphi 058/059/060 set) produced a warning
    on every single migrate run until they were removed.
    """
    from robothor.db import migrate as m

    conn = _conn()
    try:
        known = {x.migration_id for x in m._discover()}
        orphans = sorted(set(_recorded(conn.cursor())) - known)
        assert not orphans, (
            f"ledger rows with no manifest entry: {orphans}. Either restore the "
            f"file or delete the row; a permanent warning trains people to ignore "
            f"migrate output."
        )
    finally:
        conn.close()


def test_a_column_production_code_reads_actually_exists():
    """The concrete bug this class of drift produced.

    daemon.py:90 SELECTs agent_runs.resume_attempts and :119 UPDATEs it. The
    column was absent for weeks. It failed SOFT -- the SELECT is caught, logs a
    warning, and degrades to reaping, which is exactly the behaviour migration
    108 exists to remove -- and the feature is flag-gated off, so nothing broke
    loudly. A loaded gun rather than a fire.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='agent_runs' AND column_name='resume_attempts'"
        )
        assert cur.fetchone() is not None, (
            "agent_runs.resume_attempts is missing, but daemon.py reads and writes "
            "it. Apply 108_run_resume_attempts."
        )
    finally:
        conn.close()


def test_deferred_migrations_are_still_genuinely_deferred():
    """A deferral must not quietly become an application.

    If someone applies one of these, the entry here is stale and the reason
    recorded above no longer describes reality -- so fail, and make them delete
    the line deliberately.
    """
    from robothor.db import migrate as m

    conn = _conn()
    try:
        recorded = _recorded(conn.cursor())
        known = {x.migration_id for x in m._discover()}

        unknown = sorted(k for k in DEFERRED if k not in known)
        assert not unknown, f"DEFERRED names a migration the manifest lacks: {unknown}"

        # A deferral is a statement about a LONG-LIVED database. A freshly built
        # one (CI, or a rebuilt robothor_test) legitimately applies the whole
        # chain including the deferred entries, and that is not staleness -- it
        # is the chain being internally consistent, which is what CI proves.
        if known <= set(recorded):
            pytest.skip("fully-migrated database; deferrals do not apply here")

        stale = sorted(k for k in DEFERRED if k in recorded)
        assert not stale, (
            f"these are recorded as applied but still listed as DEFERRED: {stale}. "
            f"Remove them from DEFERRED so the list keeps meaning something."
        )
    finally:
        conn.close()
