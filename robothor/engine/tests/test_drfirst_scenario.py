"""Stage 4 end-to-end regression gate — DrFirst scenario.

On 2026-04-10 email-responder replied to April Rainwater's meeting-request
by scheduling a meeting, even though the parent task body said "get these
details from her without scheduling a meeting." This test proves the
objective now wins over thread etiquette:

  1. A thread with objective="... without scheduling a meeting" exists
  2. The planner, given a history of booking-link offers and no answered
     email questions, produces action="ask" with a concrete drop-or-pursue
     question — NOT action="execute" with a meeting-scheduling next action
  3. When the planner DOES execute (earlier in the sequence, when silence
     is the pattern), the spawned child receives the parent objective in
     its prompt and is told "DO NOT offer options that contradict the
     objective"
  4. The autonomy classifier refuses `calendar_send_new` for this
     objective regardless of budget

This is the test we want to stay green for the life of the feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robothor.engine.autonomy import classify_action
from robothor.engine.thread_planner import plan_thread
from robothor.engine.thread_pool import Thread

OBJECTIVE = "Confirm RxHistory pricing without scheduling a meeting"


def _thread(**kwargs) -> Thread:
    defaults = {
        "id": "drfirst-thread",
        "title": "DrFirst: confirm RxHistory pricing",
        "status": "TODO",
        "priority": "normal",
        "age_days": 10,
        "stale_days": 3,
        "requires_human": False,
        "sla_breached": False,
        "escalation_count": 0,
        "open_children": 0,
        "total_children": 0,
        "assigned_to_agent": "main",
    }
    defaults.update(kwargs)
    return Thread(**defaults)


class TestDrFirstAutonomy:
    def test_calendar_send_new_refused_regardless_of_budget(self):
        verdict = classify_action(
            "calendar_send_new",
            metadata={"objective": OBJECTIVE, "reversible": True, "estimated_cost_usd": 0},
            budget={
                "reversible_cap_usd": 10000,
                "irreversible_cap_usd": 10000,
                "categories": {"calendar_send_new": "auto"},
                "hard_floor": [],
            },
        )
        assert verdict == "refuse"


class TestDrFirstPlanner:
    def test_planner_escalates_after_repeated_booking_link_offers(self):
        """The real failure: vendor kept sending booking links, email-responder
        kept resolving tasks as "scheduling follow-up in progress", thread sat
        for 6 days. Planner should now surface a concrete ask instead."""
        history = [
            {
                "metadata": {"kind": "calendar_offer_received"},
                "created_at": datetime.now(UTC) - timedelta(hours=24),
            },
            {
                "metadata": {"kind": "calendar_offer_received"},
                "created_at": datetime.now(UTC) - timedelta(hours=72),
            },
            {
                "metadata": {"kind": "email_sent"},
                "created_at": datetime.now(UTC) - timedelta(hours=96),
            },
        ]
        plan = plan_thread(
            thread=_thread(stale_days=6, escalation_count=2),
            body=(
                "threadId: 0199c08e29\n"
                "from: april@example.com\n"
                "objective: " + OBJECTIVE + "\n"
                "\n"
                "She keeps sending booking links."
            ),
            history=history,
            autonomy={
                "reversible_cap_usd": 500,
                "irreversible_cap_usd": 0,
                "categories": {"calendar_send_new": "auto"},
                "hard_floor": [],
            },
            objective=OBJECTIVE,
        )
        assert plan.action == "ask"
        q = plan.question_for_operator or ""
        # Concrete question — not just "needs human decision"
        assert len(q) > 30
        assert "drop" in q.lower() or "continue" in q.lower() or "pursue" in q.lower()

    def test_planner_chases_vendor_after_48h_silence_before_escalating(self):
        """Before we've given up — if we sent an email 72h ago and silence,
        planner prescribes a chase, not a meeting."""
        history = [
            {
                "metadata": {"kind": "email_sent"},
                "created_at": datetime.now(UTC) - timedelta(hours=72),
            }
        ]
        plan = plan_thread(
            thread=_thread(stale_days=3, escalation_count=0),
            body=("threadId: 0199c08e29\nfrom: april@example.com\nobjective: " + OBJECTIVE + "\n"),
            history=history,
            autonomy={
                "reversible_cap_usd": 500,
                "irreversible_cap_usd": 0,
                "categories": {"vendor_data_ask": "auto"},
                "hard_floor": [],
            },
            objective=OBJECTIVE,
        )
        assert plan.action == "execute"
        assert plan.next_action_agent == "email-responder"
        action_text = (plan.next_action or "").lower()
        # Must not suggest scheduling — "no meeting" phrasing is fine, but
        # "schedule", "calendar invite", "book" all imply meeting creation.
        assert "schedule" not in action_text
        assert "calendar" not in action_text
        assert "book a" not in action_text
        # Must reference the missing datum from the objective
        assert "pricing" in action_text


class TestDrFirstSpawnContext:
    def test_parent_task_injection_warns_worker_about_objective(self):
        """When main spawns email-responder with parent_task_id, the child
        must receive the --- PARENT TASK --- block with the objective and
        a DO NOT directive. The worker's own instructions (Section 2a) then
        translate that into "refuse the meeting, redirect to email"."""
        # Inject the parent-task fetch via monkeypatch — this test is
        # deliberately unit-level; end-to-end integration is covered by
        # test_spawn_parent_context.py.
        from unittest.mock import patch

        from robothor.engine.tools.handlers.spawn import _build_parent_context_block

        parent = {
            "id": "drfirst-task-uuid",
            "title": "DrFirst: confirm RxHistory pricing",
            "objective": OBJECTIVE,
            "nextAction": "Email April asking for written pricing by EOW",
            "nextActionAgent": "email-responder",
            "questionForOperator": None,
            "autonomyBudget": {"summary": "auto under $500"},
        }
        with patch("robothor.crm.dal.get_task", return_value=parent):
            block = _build_parent_context_block("drfirst-task-uuid", tenant_id="default")

        assert block is not None
        assert OBJECTIVE in block
        assert "DO NOT" in block or "do not" in block.lower()
        assert "drfirst-task-uuid" in block
