"""A number change is not a re-report. It is the update.

WS-3's reinforce-not-fork shortcut skips the LLM classifier whenever a new fact
is >=0.92 cosine-similar to an existing one in the same category, on the theory
that it is the same event being re-reported. That theory breaks precisely where
it matters most.

Measured: "FakeVendorCo charges $100 for Meridian access." vs the same sentence
with $150 scores 0.9882 — comfortably over the threshold, same category. So the
price UPDATE was discarded and the STALE fact had its importance bumped, making
the wrong value more likely to be retrieved. MEMORY_WRITE_DEDUP=1 is live in
production, so this was happening to every fact that differs only by a number:
prices, ports, versions, dates, times, quantities.

Found by seeding the eval's temporal cases through the real write path.
"""

from __future__ import annotations

import pytest

from robothor.memory.conflicts import numbers_differ


class TestNumbersDiffer:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("FakeVendorCo charges $100 for Meridian.", "FakeVendorCo charges $150 for Meridian."),
            ("The standup is at 9am.", "The standup is at 10am."),
            ("Helios runs on port 3000.", "Helios runs on port 8080."),
            ("We are on version 2.1 of the API.", "We are on version 2.2 of the API."),
            ("The contract covers 12 seats.", "The contract covers 20 seats."),
        ],
    )
    def test_numeric_changes_are_detected(self, a, b):
        assert numbers_differ(a, b) is True

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # No numbers at all — the shortcut should still be available.
            ("Alice prefers dark mode.", "Alice prefers dark mode for coding."),
            # Same numbers, extra wording — a genuine re-report.
            ("FakeVendorCo charges $100.", "FakeVendorCo charges $100 for access."),
            # Identical.
            ("Standup is at 9am.", "Standup is at 9am."),
        ],
    )
    def test_non_numeric_rewording_is_not_flagged(self, a, b):
        assert numbers_differ(a, b) is False

    def test_reordered_same_numbers_are_not_a_change(self):
        # Order should not manufacture a difference.
        assert numbers_differ("ports 80 and 443", "ports 443 and 80") is False

    def test_formatting_of_the_same_number_is_not_a_change(self):
        # $1,200 and $1200 are the same amount written differently.
        assert numbers_differ("costs $1,200 total", "costs $1200 total") is False

    def test_empty_input_is_not_a_crash(self):
        assert numbers_differ("", "") is False
        assert numbers_differ("", "abc 5") is True


@pytest.mark.integration
class TestReinforceShortcutRespectsNumbers:
    """The wiring. The pure helper is worthless if resolve_and_store ignores it."""

    @pytest.mark.asyncio
    # Falling through the shortcut is the POINT of the fix, and the fallthrough
    # runs a real LLM classification plus two embeddings. The 30s suite default
    # is a unit-test budget; this is a live-model integration test.
    @pytest.mark.timeout(240)
    async def test_a_price_update_is_not_swallowed_as_a_reinforce(self, monkeypatch):
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection
        from robothor.memory.conflicts import resolve_and_store

        monkeypatch.setenv("MEMORY_WRITE_DEDUP", "1")  # as production runs it
        tenant = DEFAULT_TENANT
        marker = "ZzQuidditchCorp"
        old = {
            "fact_text": f"{marker} charges $100 for Meridian access.",
            "category": "pricing", "entities": [marker, "Meridian"], "confidence": 0.9,
        }
        new = {**old, "fact_text": f"{marker} charges $150 for Meridian access."}
        try:
            await resolve_and_store(old, "src", "test", tenant_id=tenant)
            result = await resolve_and_store(new, "src", "test", tenant_id=tenant)
            # It may be superseded or stored, but it must NOT be silently
            # reinforced away — the $150 has to exist somewhere.
            assert result.get("action") != "reinforced", (
                "a numeric update was swallowed by the reinforce shortcut"
            )
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT count(*) FROM memory_facts "
                    "WHERE tenant_id = %s AND fact_text LIKE %s AND is_active",
                    (tenant, f"%{marker}%$150%"),
                )
                assert cur.fetchone()[0] == 1, "the current price is not in active memory"
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM memory_facts WHERE tenant_id = %s AND fact_text LIKE %s",
                    (tenant, f"%{marker}%"),
                )
                conn.commit()
