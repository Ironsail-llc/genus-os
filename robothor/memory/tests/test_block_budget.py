"""Tier 1 context budget: enforce max_chars, and measure what it costs.

`max_chars` is written at seed time (blocks.py DEFAULT_BLOCK_SEEDS) and read by
nothing. Measured on the live table: 130 of 2,492 blocks exceed their own
declared budget, the largest is 57,051 characters — roughly 14k tokens in a
single always-loaded block — and 2,092 have never been read even once.

That is the failure the field has already documented. Claude Code enforces
200 lines / 25 KB on its always-loaded index and rejects over-budget writes;
Cline's eager load reaches ~300k tokens after five iterations and is conceded
as a defect. We had the column and no enforcement, which is the worst of both:
the appearance of a budget with none of the effect.

Overflow must not be silently truncated — truncation deletes the end of a block
without telling anyone, and the end is where recent context lives.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from robothor.memory.block_budget import (
    DEFAULT_MAX_CHARS,
    check_budget,
    estimate_tokens,
    tier_token_report,
)


class TestCheckBudget:
    def test_under_budget_passes(self):
        v = check_budget("short", max_chars=100)
        assert v.over is False
        assert v.overflow_chars == 0

    def test_over_budget_is_flagged_with_the_amount(self):
        v = check_budget("x" * 150, max_chars=100)
        assert v.over is True
        assert v.overflow_chars == 50
        # The caller needs to know how much to cut, not just that it must.
        assert "150" in v.reason and "100" in v.reason

    def test_exactly_at_budget_is_allowed(self):
        assert check_budget("x" * 100, max_chars=100).over is False

    def test_null_max_chars_falls_back_to_a_real_default(self):
        # 2,492 rows exist and many predate the seed defaults; NULL must not
        # mean "unlimited", which is how a 57k-char block happened.
        v = check_budget("x" * (DEFAULT_MAX_CHARS + 1), max_chars=None)
        assert v.over is True

    def test_zero_and_negative_max_chars_are_treated_as_unset(self):
        # A misconfigured 0 must not reject every write.
        assert check_budget("hello", max_chars=0).over is False
        assert check_budget("hello", max_chars=-5).over is False

    def test_empty_content_is_never_over(self):
        assert check_budget("", max_chars=10).over is False

    def test_verdict_is_immutable(self):
        with pytest.raises(FrozenInstanceError):
            check_budget("x", max_chars=10).over = True


class TestEstimateTokens:
    def test_roughly_four_chars_per_token(self):
        assert estimate_tokens("x" * 400) == 100

    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_never_negative(self):
        assert estimate_tokens("ab") >= 0


class TestTierTokenReport:
    """The number this whole overhaul optimises, which did not exist."""

    def test_reports_tokens_per_tier(self):
        rows = [
            {"block_name": "persona", "block_type": "core", "content": "x" * 400},
            {"block_name": "scratch", "block_type": "working", "content": "y" * 800},
            {"block_name": "other", "block_type": "working", "content": "z" * 400},
        ]
        rep = tier_token_report(rows)
        assert rep["by_tier"]["core"]["tokens"] == 100
        assert rep["by_tier"]["working"]["tokens"] == 300
        assert rep["total_tokens"] == 400

    def test_counts_blocks_per_tier(self):
        rows = [
            {"block_name": "a", "block_type": "core", "content": "x"},
            {"block_name": "b", "block_type": "core", "content": "x"},
        ]
        assert tier_token_report(rows)["by_tier"]["core"]["blocks"] == 2

    def test_missing_type_lands_in_untyped_not_a_crash(self):
        rep = tier_token_report([{"block_name": "a", "content": "x" * 400}])
        assert rep["by_tier"]["untyped"]["tokens"] == 100

    def test_over_budget_blocks_are_named(self):
        rows = [
            {"block_name": "huge", "block_type": "core", "content": "x" * 900, "max_chars": 100},
            {"block_name": "fine", "block_type": "core", "content": "x", "max_chars": 100},
        ]
        rep = tier_token_report(rows)
        assert [b["block_name"] for b in rep["over_budget"]] == ["huge"]
        assert rep["over_budget"][0]["overflow_chars"] == 800

    def test_empty_input_is_a_zero_report_not_a_crash(self):
        rep = tier_token_report([])
        assert rep["total_tokens"] == 0
        assert rep["by_tier"] == {}
        assert rep["over_budget"] == []


class TestWriteBlockRefusesOverflow:
    """The enforcement itself. Without this the rest is just a report."""

    @pytest.mark.integration
    def test_over_budget_write_is_rejected_not_truncated(self, monkeypatch):
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection
        from robothor.memory.blocks import write_block

        monkeypatch.setenv("MEMORY_BLOCK_BUDGET", "enforce")
        name = "budget-enforcement-probe"
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO agent_memory_blocks (tenant_id, block_name, content, max_chars) "
                "VALUES (%s, %s, 'seed', 40) "
                "ON CONFLICT (tenant_id, block_name) DO UPDATE SET max_chars = 40, "
                "content = 'seed'",
                (DEFAULT_TENANT, name),
            )
            conn.commit()
        try:
            result = write_block(name, "y" * 500)
            assert result.get("error"), "an over-budget write must be refused"
            assert "budget" in result["error"].lower()

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT content FROM agent_memory_blocks "
                    "WHERE tenant_id = %s AND block_name = %s",
                    (DEFAULT_TENANT, name),
                )
                stored = cur.fetchone()[0]
            # Not truncated, not partially applied — the previous content stands.
            assert stored == "seed"
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM agent_memory_blocks WHERE tenant_id = %s AND block_name = %s",
                    (DEFAULT_TENANT, name),
                )
                conn.commit()

    @pytest.mark.integration
    def test_observe_mode_allows_the_write_but_records_it(self, monkeypatch):
        # The ladder's middle rung: 130 blocks are already over budget, so
        # flipping straight to enforce would break real writes on day one.
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection
        from robothor.memory.blocks import write_block

        monkeypatch.setenv("MEMORY_BLOCK_BUDGET", "observe")
        name = "budget-observe-probe"
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO agent_memory_blocks (tenant_id, block_name, content, max_chars) "
                "VALUES (%s, %s, 'seed', 40) "
                "ON CONFLICT (tenant_id, block_name) DO UPDATE SET max_chars = 40",
                (DEFAULT_TENANT, name),
            )
            conn.commit()
        try:
            result = write_block(name, "y" * 500)
            assert result.get("success") is True
            assert result.get("over_budget") is True
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM agent_memory_blocks WHERE tenant_id = %s AND block_name = %s",
                    (DEFAULT_TENANT, name),
                )
                conn.commit()

    @pytest.mark.integration
    def test_default_off_does_not_change_behaviour(self, monkeypatch):
        # 2,492 live blocks; a budget that silently turns on is an outage.
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection
        from robothor.memory.blocks import write_block

        monkeypatch.delenv("MEMORY_BLOCK_BUDGET", raising=False)
        name = "budget-off-probe"
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO agent_memory_blocks (tenant_id, block_name, content, max_chars) "
                "VALUES (%s, %s, 'seed', 40) "
                "ON CONFLICT (tenant_id, block_name) DO UPDATE SET max_chars = 40",
                (DEFAULT_TENANT, name),
            )
            conn.commit()
        try:
            result = write_block(name, "y" * 500)
            assert result.get("success") is True
            assert "error" not in result
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM agent_memory_blocks WHERE tenant_id = %s AND block_name = %s",
                    (DEFAULT_TENANT, name),
                )
                conn.commit()


@pytest.mark.integration
class TestPrunedBlocksLeaveTheContext:
    """A prune the read path ignores is decoration.

    1,525 blocks were soft-deleted to reclaim 4.09M characters from the
    always-loaded tier. If list_blocks still enumerates them and read_block
    still serves them, not one token was saved and the whole exercise was a
    column update.
    """

    def _mk(self, name, pruned):
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO agent_memory_blocks (tenant_id, block_name, content, pruned_at) "
                "VALUES (%s, %s, 'body', %s) "
                "ON CONFLICT (tenant_id, block_name) DO UPDATE "
                "SET pruned_at = EXCLUDED.pruned_at, content = 'body'",
                (DEFAULT_TENANT, name, "NOW()" if False else (None if not pruned else "now")),
            )
            if pruned:
                cur.execute(
                    "UPDATE agent_memory_blocks SET pruned_at = NOW() "
                    "WHERE tenant_id = %s AND block_name = %s",
                    (DEFAULT_TENANT, name),
                )
            conn.commit()

    def _rm(self, *names):
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM agent_memory_blocks WHERE tenant_id = %s AND block_name = ANY(%s)",
                (DEFAULT_TENANT, list(names)),
            )
            conn.commit()

    def test_pruned_block_is_not_listed_but_live_one_is(self):
        from robothor.memory.blocks import list_blocks

        live, dead = "prune-probe-live", "prune-probe-dead"
        self._mk(live, pruned=False)
        self._mk(dead, pruned=True)
        try:
            names = {b["name"] for b in list_blocks()["blocks"]}
            assert live in names, "negative control: a live block must still list"
            assert dead not in names, "a pruned block must leave the always-loaded tier"
        finally:
            self._rm(live, dead)

    def test_pruned_block_is_not_readable(self):
        from robothor.memory.blocks import read_block

        dead = "prune-probe-read"
        self._mk(dead, pruned=True)
        try:
            assert read_block(dead).get("error"), "a pruned block must not be served"
        finally:
            self._rm(dead)
