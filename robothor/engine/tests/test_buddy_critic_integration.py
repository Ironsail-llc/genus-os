"""Integration tests for buddy_critic against the real agent_reviews table.

These catch DB-constraint bugs that unit mocks cannot — specifically the
`reviewer_type='buddy'` CHECK constraint that silently dropped reviews on
the first live run before migration 036.

Run with:
    pytest robothor/engine/tests/test_buddy_critic_integration.py -m integration
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from robothor.engine.buddy_critic import Evidence, Review, persist_review

pytestmark = pytest.mark.integration


def _ev() -> Evidence:
    return Evidence(
        run_id="00000000-0000-0000-0000-000000000000",
        agent_id="__test_buddy_agent__",
        status="completed",
        started_at=datetime.now(UTC),
        duration_ms=1000,
        total_cost_usd=0.01,
        output_text_truncated="integration test body",
        error_message=None,
        error_steps=[],
        tool_call_count=1,
        tool_error_count=0,
    )


class TestPersistReviewAgainstLiveDB:
    def test_buddy_reviewer_type_accepted(self):
        """Regression for the CHECK constraint bug.

        The constraint previously only allowed operator/agent/system;
        migration 036 added 'buddy'. This test will fail immediately if
        that migration is reverted or if reviewer_type is renamed.
        """
        from robothor.crm.dal import get_reviews
        from robothor.db.connection import get_connection

        review = Review(
            agent_id="__test_buddy_agent__",
            run_id="00000000-0000-0000-0000-000000000000",
            rating=3,
            dimension="correctness",
            specific_issue="integration-test issue with concrete anchor",
            suggested_action="integration-test action",
            raw_evidence=_ev(),
        )

        review_id = None
        try:
            review_id = persist_review(review)
            assert review_id is not None, (
                "persist_review returned None — the CHECK constraint is "
                "probably back to operator/agent/system only. Check migration 036."
            )

            # Round-trip: the row is readable via get_reviews and carries the
            # categories dict we wrote.
            rows = get_reviews("__test_buddy_agent__", days=1)
            our_row = next((r for r in rows if r.get("id") == review_id), None)
            assert our_row is not None
            # Feedback should start with the specific issue we wrote.
            assert "integration-test issue" in (our_row.get("feedback") or "")
            assert our_row.get("reviewer_type") == "buddy"
        finally:
            if review_id:
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM agent_reviews WHERE id = %s",
                        (review_id,),
                    )
                    conn.commit()
