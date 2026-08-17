"""Write-time quality gate: what the system agrees to remember.

Everything reaching store_fact is stored; there is no bar. A retrieval system's
precision is bounded by what it agreed to remember — junk is not neutral, it
occupies the top-k, and the top-k is the product.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from robothor.memory.quality import (
    MAX_CHARS,
    MIN_CHARS,
    quality_mode,
    score_fact,
)


class TestScoreFact:
    def test_a_real_fact_is_accepted(self):
        v = score_fact("Alice manages the Helios project at FakeVendorCo.")
        assert v.accept is True
        assert v.reasons == ()

    def test_fragment_is_rejected(self):
        v = score_fact("yes")
        assert v.accept is False
        assert any("too_short" in r for r in v.reasons)

    def test_document_is_rejected(self):
        v = score_fact("x " * MAX_CHARS)
        assert any("too_long" in r for r in v.reasons)

    def test_exactly_at_the_bounds_is_accepted(self):
        assert score_fact("a" * MIN_CHARS).accept is True
        assert score_fact("a" * MAX_CHARS).accept is True

    def test_no_letters_is_rejected(self):
        assert "no_letters" in score_fact("--- 12345 --- 67890 ---").reasons

    @pytest.mark.parametrize(
        "text",
        [
            "I will look into the Helios project timeline for you now.",
            "Let me check the current status of that vendor contract.",
            "Sure, here is the summary of what we discussed in the meeting.",
            "As an AI, I don't have access to that particular record here.",
        ],
    )
    def test_agent_chatter_is_rejected(self, text):
        # The most common junk shape: the agent narrating its own turn instead
        # of recording something about the world.
        assert "agent_chatter" in score_fact(text).reasons

    def test_a_fact_that_merely_starts_with_i_is_not_chatter(self):
        # Negative control. Over-matching here would silently discard real
        # first-person facts, which is worse than the junk it prevents.
        assert score_fact("Iceland raised its policy rate to 4.5% in March.").accept
        assert score_fact("Ian Chen leads the Meridian integration workstream.").accept

    def test_low_confidence_is_rejected(self):
        assert any("low_confidence" in r for r in score_fact("A" * 40, confidence=0.1).reasons)

    def test_absent_confidence_is_not_a_rejection(self):
        # None means unknown, not zero.
        assert score_fact("A" * 40, confidence=None).accept is True

    def test_all_failing_reasons_are_reported_not_just_the_first(self):
        # A shadow soak needs to know WHICH rule does the work.
        v = score_fact("hi", confidence=0.0)
        assert len(v.reasons) >= 2

    def test_verdict_is_immutable(self):
        with pytest.raises(FrozenInstanceError):
            score_fact("A" * 40).accept = False


class TestMode:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("MEMORY_QUALITY_GATE", raising=False)
        assert quality_mode() == "off"

    def test_unknown_value_is_off_not_enforce(self, monkeypatch):
        # A typo must fail safe toward storing, never toward discarding data.
        monkeypatch.setenv("MEMORY_QUALITY_GATE", "enforc")
        assert quality_mode() == "off"

    def test_shadow_and_enforce_are_read(self, monkeypatch):
        monkeypatch.setenv("MEMORY_QUALITY_GATE", "shadow")
        assert quality_mode() == "shadow"
        monkeypatch.setenv("MEMORY_QUALITY_GATE", "ENFORCE")
        assert quality_mode() == "enforce"


class TestAgainstLiveData:
    """The gate applied to what is actually in the table."""

    @pytest.mark.integration
    def test_rejection_rate_on_live_facts_is_sane(self):
        # If this gate would reject a large fraction of real memory, the rules
        # are wrong, not the data. Enforcing it would then be an outage.
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT fact_text, confidence FROM memory_facts "
                "WHERE is_active ORDER BY id DESC LIMIT 2000"
            )
            rows = cur.fetchall()
        if not rows:
            pytest.skip("no active facts")
        rejected = [r for r in rows if not score_fact(r[0], confidence=r[1]).accept]
        rate = len(rejected) / len(rows)
        assert rate < 0.10, (
            f"gate would reject {rate:.1%} of live facts — the rules are too aggressive to enforce"
        )


@pytest.mark.integration
class TestGateIsActuallyWired:
    """A gate that scores correctly and is never called is worth nothing.
    Fires a real violation through the real write path in each mode.
    """

    FACT = {"fact_text": "no", "category": "other", "entities": [], "confidence": 0.9}

    @pytest.mark.asyncio
    async def test_enforce_refuses_the_write(self, monkeypatch):
        from robothor.memory.facts import store_fact

        monkeypatch.setenv("MEMORY_QUALITY_GATE", "enforce")
        fact_id = await store_fact(dict(self.FACT), "src", "test")
        assert fact_id == 0, "an enforced gate must refuse a fragment"

    @pytest.mark.asyncio
    async def test_off_stores_it(self, monkeypatch):
        # Negative control: without this the test above only proves the write
        # path is broken, not that the gate did the refusing.
        from robothor.db.connection import get_connection
        from robothor.memory.facts import store_fact

        monkeypatch.delenv("MEMORY_QUALITY_GATE", raising=False)
        fact_id = await store_fact(dict(self.FACT), "src", "test")
        try:
            assert fact_id and fact_id > 0
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM memory_facts WHERE id = %s", (fact_id,))
                conn.commit()

    @pytest.mark.asyncio
    async def test_shadow_stores_it_and_records_the_rejection(self, monkeypatch):
        from robothor.db.connection import get_connection
        from robothor.memory.facts import store_fact

        monkeypatch.setenv("MEMORY_QUALITY_GATE", "shadow")
        fact_id = await store_fact(dict(self.FACT), "src", "test")
        try:
            assert fact_id and fact_id > 0, "shadow must not refuse"
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT reason, snapshot FROM memory_facts_audit "
                    "WHERE fact_id = %s AND reason = 'quality_would_reject'",
                    (fact_id,),
                )
                row = cur.fetchone()
            assert row is not None, "shadow must leave evidence, not silence"
            assert any("too_short" in r for r in row[1]["reasons"])
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM memory_facts_audit WHERE fact_id = %s", (fact_id,))
                cur.execute("DELETE FROM memory_facts WHERE id = %s", (fact_id,))
                conn.commit()
