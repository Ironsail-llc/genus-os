"""The detector must not itself be a hollow control.

Two guarantees: (1) every declared evidence source names a table that actually
exists and is queryable — else the detector reads a missing table and reports a
comforting zero; (2) a genuinely-inert control (human_approval: enforce, zero
events ever) comes back INERT, tested against the live table, not a mock.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from robothor.flags import evidence
from robothor.flags.store import GOVERNED_FLAGS

pytestmark = pytest.mark.integration  # uses db_cursor (robothor_test), like Tasks 1-2


def _use_test_db(db_cursor, monkeypatch):
    """Point evidence.get_connection at db_cursor's own connection, so verdict()
    reads (and any seed writes) stay inside this test's transaction — real SQL
    against the live table, rolled back on teardown, never a mock and never
    production."""

    @contextmanager
    def _conn(autocommit: bool = False):
        yield db_cursor.connection

    monkeypatch.setattr(evidence, "get_connection", _conn)


def test_every_governed_flag_has_an_evidence_source():
    assert set(evidence.EVIDENCE_SOURCES) == set(GOVERNED_FLAGS)


def test_every_evidence_source_table_exists(db_cursor):
    missing = []
    for name, src in evidence.EVIDENCE_SOURCES.items():
        db_cursor.execute("SELECT to_regclass(%s)", (f"public.{src.table}",))
        if db_cursor.fetchone()["to_regclass"] is None:
            missing.append(f"{name}: table {src.table} missing")
    assert not missing, "evidence source table(s) not present in this DB:\n" + "\n".join(missing)


def test_enforce_with_zero_evidence_is_inert(db_cursor, monkeypatch):
    # human_approval genuinely has zero events on a fresh instance.
    _use_test_db(db_cursor, monkeypatch)
    v = evidence.verdict("ROBOTHOR_APPROVAL_MODE", "enforce")
    assert v.status == "INERT"
    assert "NEVER FIRED" in v.message.upper()


def test_enforce_with_recent_evidence_is_enforcing(db_cursor, monkeypatch):
    # injection_scan has real blocks; if this instance has none in 7d it is
    # UNPROVEN, which is also acceptable — assert it is NOT falsely INERT when
    # evidence exists. Seed one real block so the "evidence exists" path is
    # actually exercised, not just hoped for.
    _use_test_db(db_cursor, monkeypatch)
    db_cursor.execute(
        "INSERT INTO agent_runs (agent_id, trigger_type, status) "
        "VALUES ('main', 'manual', 'completed') RETURNING id"
    )
    run_id = db_cursor.fetchone()["id"]
    db_cursor.execute(
        "INSERT INTO agent_guardrail_events "
        "(run_id, step_number, guardrail_name, action) "
        "VALUES (%s, 1, 'injection_scan', 'blocked')",
        (run_id,),
    )
    v = evidence.verdict("ROBOTHOR_INJECTION_SCAN_MODE", "enforce")
    assert v.status in {"ENFORCING", "UNPROVEN"}


def test_off_mode_is_unproven_not_inert(db_cursor, monkeypatch):
    # A flag that is simply off should never be reported as a dead ENFORCE
    # control — "disabled" and "never fired despite being told to enforce"
    # are different findings and must not collapse into the same status.
    _use_test_db(db_cursor, monkeypatch)
    v = evidence.verdict("ROBOTHOR_RBAC_MODE", "off")
    assert v.status == "UNPROVEN"
    assert v.message == "disabled"
