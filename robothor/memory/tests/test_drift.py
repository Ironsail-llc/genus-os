"""Tests for `robothor.memory.drift` (Rip 7 drift detector helper)."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import MagicMock, patch

from robothor.memory.drift import (
    DriftDecision,
    audit_snapshot,
    compute_fact_hash,
    evaluate_drift,
)


class TestComputeFactHash:
    def test_returns_64_hex_chars(self) -> None:
        h = compute_fact_hash("hello", tenant_id="t1")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self) -> None:
        a = compute_fact_hash("x", tenant_id="t", category="c", person_id="p")
        b = compute_fact_hash("x", tenant_id="t", category="c", person_id="p")
        assert a == b

    def test_text_change_changes_hash(self) -> None:
        a = compute_fact_hash("alpha", tenant_id="t")
        b = compute_fact_hash("beta", tenant_id="t")
        assert a != b

    def test_tenant_isolation(self) -> None:
        a = compute_fact_hash("x", tenant_id="t1")
        b = compute_fact_hash("x", tenant_id="t2")
        assert a != b, "facts in different tenants must hash differently"

    def test_category_changes_hash(self) -> None:
        a = compute_fact_hash("x", tenant_id="t", category="preference")
        b = compute_fact_hash("x", tenant_id="t", category="event")
        assert a != b

    def test_person_changes_hash(self) -> None:
        a = compute_fact_hash("x", tenant_id="t", person_id="alice")
        b = compute_fact_hash("x", tenant_id="t", person_id="bob")
        assert a != b

    def test_missing_optional_is_empty_string(self) -> None:
        with_explicit_blanks = compute_fact_hash("x", tenant_id="t", category="", person_id=None)
        with_minimum_args = compute_fact_hash("x", tenant_id="t")
        assert with_explicit_blanks == with_minimum_args

    def test_no_field_swap_collision(self) -> None:
        # tenant 'A' + category 'B' must not hash like tenant 'B' + category 'A'
        a = compute_fact_hash("x", tenant_id="A", category="B")
        b = compute_fact_hash("x", tenant_id="B", category="A")
        assert a != b

    def test_matches_postgres_digest_formula(self) -> None:
        # The migration backfill uses
        # `digest(text || '|' || tenant || '|' || category || '|' || person, 'sha256')`
        # encoded as hex. This test pins the Python side to the
        # same formula so drift between the two implementations
        # surfaces immediately.
        text, tenant, category, person = ("the fact", "t1", "preference", "alice")
        canonical = f"{text}|{tenant}|{category}|{person}"
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert (
            compute_fact_hash(text, tenant_id=tenant, category=category, person_id=person)
            == expected
        )


class TestEvaluateDrift:
    def _hash(self, text: str = "x", tenant: str = "t") -> str:
        return compute_fact_hash(text, tenant_id=tenant)

    def test_off_mode_proceeds_without_check(self) -> None:
        with patch.dict(os.environ, {}, clear=True):  # rip 7 disabled
            decision = evaluate_drift("bogus_hash", fact_text="x", tenant_id="t")
            assert decision == DriftDecision(action="proceed", drift_detected=False, mode="off")

    def test_null_stored_hash_proceeds(self) -> None:
        # First touch since migration — stored_hash is NULL — must not
        # be treated as drift.
        with patch.dict(os.environ, {"ROBOTHOR_RIP_7_ENABLED": "1"}, clear=True):
            decision = evaluate_drift(None, fact_text="x", tenant_id="t")
            assert decision.action == "proceed"
            assert decision.drift_detected is False

    def test_matching_hash_proceeds(self) -> None:
        good = self._hash()
        with patch.dict(os.environ, {"ROBOTHOR_RIP_7_ENABLED": "1"}, clear=True):
            decision = evaluate_drift(good, fact_text="x", tenant_id="t")
            assert decision.action == "proceed"
            assert decision.drift_detected is False

    def test_drift_in_observe_mode_proceeds_with_flag(self) -> None:
        wrong = "0" * 64
        with patch.dict(os.environ, {"ROBOTHOR_RIP_7_ENABLED": "1"}, clear=True):
            decision = evaluate_drift(wrong, fact_text="x", tenant_id="t")
            assert decision.action == "proceed"
            assert decision.drift_detected is True
            assert decision.mode == "observe"

    def test_drift_in_alert_mode_proceeds_with_flag(self) -> None:
        wrong = "0" * 64
        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "alert"},
            clear=True,
        ):
            decision = evaluate_drift(wrong, fact_text="x", tenant_id="t")
            assert decision.action == "proceed"
            assert decision.drift_detected is True
            assert decision.mode == "alert"

    def test_drift_in_enforce_mode_refuses(self) -> None:
        wrong = "0" * 64
        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "enforce"},
            clear=True,
        ):
            decision = evaluate_drift(wrong, fact_text="x", tenant_id="t")
            assert decision.action == "refuse"
            assert decision.drift_detected is True
            assert decision.mode == "enforce"


class TestAuditSnapshot:
    def test_inserts_row_and_returns_id(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = (42,)
        result = audit_snapshot(
            cur,
            fact_id=7,
            tenant_id="t",
            fact_text="the original",
            hash_at_snapshot="abc",
            hash_expected="def",
            reason="pre_update_drift_detected",
        )
        assert result == 42
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "INSERT INTO memory_facts_audit" in sql
        assert params == (7, "t", "the original", "abc", "def", "pre_update_drift_detected")

    def test_returns_none_on_db_failure(self) -> None:
        cur = MagicMock()
        cur.execute.side_effect = RuntimeError("connection lost")
        result = audit_snapshot(
            cur,
            fact_id=1,
            tenant_id="t",
            fact_text="x",
            hash_at_snapshot=None,
            hash_expected=None,
            reason="other",
        )
        # Audit is best-effort — never raise; the calling writer must
        # still get a chance to respond to the model.
        assert result is None

    def test_handles_no_returning_row(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = None
        result = audit_snapshot(
            cur,
            fact_id=1,
            tenant_id="t",
            fact_text="x",
            hash_at_snapshot=None,
            hash_expected=None,
            reason="other",
        )
        assert result is None
