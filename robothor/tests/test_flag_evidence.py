"""The detector must not itself be a hollow control.

Three guarantees: (1) verdict() returns a valid Status for EVERY governed
flag without ever raising — whether its evidence table exists in this
database or not, because table presence is deploy-specific (``agent_reviews``
comes from an external infra migration; a simulated absent table stands in for one that
is genuinely missing) and a detector that crashes on a missing table
is itself a hollow control; (2) a genuinely-inert control (human_approval:
enforce, zero events ever) comes back INERT, tested against the live table,
not a mock; (3) a flag whose evidence table is genuinely absent here comes
back UNKNOWN — loud, never green, never ENFORCING.
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


def test_verdict_never_raises_for_any_governed_flag(db_cursor, monkeypatch):
    """Table presence is deploy-specific — some DBs have agent_reviews (an
    external infra migration), some don't; verdict() must classify every flag without raising,
    regardless of which evidence tables happen to exist here."""
    _use_test_db(db_cursor, monkeypatch)
    allowed_statuses = {"ENFORCING", "INERT", "BLIND", "UNPROVEN", "UNKNOWN"}
    for name in evidence.EVIDENCE_SOURCES:
        for mode in ("enforce", "warn", "off"):
            v = evidence.verdict(name, mode)
            assert v.status in allowed_statuses, f"{name}/{mode}: unexpected status {v.status!r}"


def test_enforce_with_zero_evidence_is_inert(db_cursor, monkeypatch):
    # human_approval genuinely has zero events on a fresh instance.
    _use_test_db(db_cursor, monkeypatch)
    v = evidence.verdict("ROBOTHOR_APPROVAL_MODE", "enforce")
    assert v.status == "INERT"
    assert "NEVER FIRED" in v.message.upper()


def test_missing_evidence_table_is_unknown_not_a_crash(db_cursor, monkeypatch):
    # verdict() must neither raise nor report anything green when the table
    # backing a control's evidence is absent — it must say, loudly, that the
    # control cannot be assessed.
    #
    # This used to assert that memory_facts_audit was genuinely missing from
    # robothor_test, treating a drifted database as the premise. That made the
    # test pass only on a stale DB and fail on a correctly-migrated one, which
    # is backwards. The absence is now simulated by pointing the lookup at a
    # table that will never exist, so the test asserts behaviour rather than
    # schema drift.
    _use_test_db(db_cursor, monkeypatch)
    monkeypatch.setitem(
        evidence.EVIDENCE_SOURCES,
        "ROBOTHOR_RIP_7_MODE",
        evidence.EvidenceSource("__absent_evidence_table__", "TRUE"),
    )
    v = evidence.verdict("ROBOTHOR_RIP_7_MODE", "enforce")
    assert v.status == "UNKNOWN"
    assert "__absent_evidence_table__" in v.message
    assert v.last_fired is None
    assert v.count_7d == 0


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
