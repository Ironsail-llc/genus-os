"""Tests for cross-request breadcrumbs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from robothor.memory.breadcrumbs import format_breadcrumbs_for_warmup


class TestFormatForWarmup:
    def test_empty_returns_empty_string(self):
        assert format_breadcrumbs_for_warmup([]) == ""

    def test_includes_header_and_content(self):
        bcs = [
            {
                "created_at": datetime(2026, 4, 16, 10, 0, tzinfo=UTC),
                "content": {"note": "Started investigation X; found Y"},
            }
        ]
        out = format_breadcrumbs_for_warmup(bcs)
        assert "Breadcrumbs from prior runs" in out
        assert "Started investigation X" in out

    def test_handles_json_string_content(self):
        bcs = [
            {
                "created_at": datetime(2026, 4, 16, tzinfo=UTC),
                "content": '{"note": "from JSON string"}',
            }
        ]
        out = format_breadcrumbs_for_warmup(bcs)
        assert "from JSON string" in out

    def test_caps_output_lines(self):
        bcs = [
            {
                "created_at": datetime(2026, 4, 16, tzinfo=UTC),
                "content": {"note": f"Note {i}"},
            }
            for i in range(20)
        ]
        out = format_breadcrumbs_for_warmup(bcs)
        # header + up to 9 lines cap = 10 lines
        assert len(out.splitlines()) <= 10


@pytest.mark.integration
class TestRoundtripDAL:
    """End-to-end insert → load → prune against real DB."""

    @pytest.mark.asyncio
    async def test_leave_load_prune_cycle(self):
        from robothor.db.connection import get_connection
        from robothor.memory.breadcrumbs import (
            leave_breadcrumb,
            load_recent_breadcrumbs,
            prune_expired_breadcrumbs,
        )

        test_agent = "__test_breadcrumb_agent__"

        try:
            bc_id = leave_breadcrumb(
                test_agent,
                {"note": "integration test crumb"},
                run_id="__test_run__",
                ttl_days=1,
            )
            assert bc_id > 0

            loaded = load_recent_breadcrumbs(test_agent, limit=5)
            assert any(bc["id"] == bc_id for bc in loaded)

            # Force expiry: move both created_at and expires_at into the past
            # so the CHECK(expires_at > created_at) constraint still holds.
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE agent_breadcrumbs
                    SET created_at = NOW() - INTERVAL '2 hours',
                        expires_at = NOW() - INTERVAL '1 hour'
                    WHERE id = %s
                    """,
                    (bc_id,),
                )

            pruned = prune_expired_breadcrumbs()
            assert pruned >= 1

            loaded_after = load_recent_breadcrumbs(test_agent)
            assert not any(bc["id"] == bc_id for bc in loaded_after)
        finally:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM agent_breadcrumbs WHERE agent_id = %s",
                    (test_agent,),
                )
