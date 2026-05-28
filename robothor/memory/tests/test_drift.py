"""Tests for `robothor.memory.drift` (Rip 7 drift detector helper)."""

from __future__ import annotations

import hashlib

from robothor.memory.drift import compute_fact_hash


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
