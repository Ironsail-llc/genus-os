"""Stage 4 — forward thread planner.

The thread planner takes a stalled thread and decides what should happen
next: execute a specific sub-agent spawn, ask the operator a concrete
question, wait, or close. Heuristic-only in v1: reads crm_task_history
and body patterns, no LLM calls.

Distinct from robothor/engine/planner.py which is a per-run LLM planner.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from robothor.engine.thread_pool import Thread


def _make_thread(
    *,
    id: str = "thread-1",
    title: str = "DrFirst: confirm RxHistory pricing",
    status: str = "TODO",
    priority: str = "normal",
    age_days: int = 10,
    stale_days: int = 3,
    requires_human: bool = False,
    sla_breached: bool = False,
    escalation_count: int = 0,
    open_children: int = 0,
    total_children: int = 0,
    assigned_to_agent: str | None = "main",
) -> Thread:
    return Thread(
        id=id,
        title=title,
        status=status,
        priority=priority,
        age_days=age_days,
        stale_days=stale_days,
        requires_human=requires_human,
        sla_breached=sla_breached,
        escalation_count=escalation_count,
        open_children=open_children,
        total_children=total_children,
        assigned_to_agent=assigned_to_agent,
    )


class TestPlanResult:
    def test_planresult_is_a_frozen_dataclass(self):
        from robothor.engine.thread_planner import PlanResult

        r = PlanResult(
            task_id="t1",
            action="execute",
            next_action="do a thing",
            next_action_agent="email-responder",
            question_for_operator=None,
            rationale="because",
        )
        assert r.task_id == "t1"
        assert r.action == "execute"
        try:
            r.task_id = "t2"  # type: ignore[misc]
            raise AssertionError("expected FrozenInstanceError")
        except Exception:
            pass


class TestPlanThreadHeuristic:
    def test_plan_infers_email_chase_when_last_action_was_send_and_72h_passed(self):
        """DrFirst pattern: email sent 72h ago, no reply, objective unmet —
        planner prescribes chasing the vendor for the missing datum."""
        from robothor.engine.thread_planner import plan_thread

        thread = _make_thread(stale_days=3, escalation_count=0)
        body = (
            "threadId: 0199c08e29\n"
            "from: april@example.com\n"
            "objective: Confirm RxHistory pricing without scheduling a meeting.\n"
            "\n"
            "Waiting on written pricing from April.\n"
        )
        history = [
            {
                "metadata": {"kind": "email_sent"},
                "created_at": datetime.now(UTC) - timedelta(hours=72),
            }
        ]
        autonomy = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 0,
            "categories": {"vendor_data_ask": "auto"},
            "hard_floor": [],
        }

        plan = plan_thread(
            thread=thread,
            body=body,
            history=history,
            autonomy=autonomy,
            objective="Confirm RxHistory pricing without scheduling a meeting",
        )

        assert plan.action == "execute"
        assert plan.next_action_agent == "email-responder"
        assert plan.next_action is not None
        assert "pricing" in plan.next_action.lower()

    def test_plan_refuses_when_objective_vetoes_the_only_path(self):
        """Vendor keeps sending booking links. Objective forbids meetings.
        Planner must ask the operator a concrete drop-or-pursue question."""
        from robothor.engine.thread_planner import plan_thread

        thread = _make_thread(stale_days=5, escalation_count=2)
        objective = "Confirm RxHistory pricing without scheduling a meeting"
        body = (
            "threadId: ABC\n"
            "from: april@example.com\n"
            "Latest reply: here is my Outlook booking link, pick a time."
        )
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
        autonomy = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 0,
            "categories": {"calendar_send_new": "auto"},
            "hard_floor": [],
        }

        plan = plan_thread(
            thread=thread,
            body=body,
            history=history,
            autonomy=autonomy,
            objective=objective,
        )

        assert plan.action == "ask"
        assert plan.question_for_operator is not None
        assert len(plan.question_for_operator) > 10

    def test_plan_skips_when_question_already_pending(self):
        from robothor.engine.thread_planner import plan_thread

        thread = _make_thread(requires_human=True, status="REVIEW")
        plan = plan_thread(
            thread=thread,
            body="",
            history=[],
            autonomy={},
            objective="whatever",
            question_for_operator="Drop DrFirst? y/n",
        )
        assert plan.action == "wait"

    def test_plan_skips_when_fresh_plan_exists(self):
        from robothor.engine.thread_planner import plan_thread

        thread = _make_thread(stale_days=0, escalation_count=0)
        plan = plan_thread(
            thread=thread,
            body="",
            history=[],
            autonomy={},
            objective="do stuff",
            next_action="already planned",
            last_planned_at=datetime.now(UTC),
        )
        assert plan.action == "wait"


class TestApplyPlan:
    def test_apply_plan_execute_sets_next_action(self):
        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t1",
            action="execute",
            next_action="chase vendor for pricing",
            next_action_agent="email-responder",
            question_for_operator=None,
            rationale="last email was 72h ago with no reply",
        )
        with patch("robothor.crm.dal.set_next_action") as m:
            m.return_value = True
            apply_plan(plan, tenant_id="default")
            m.assert_called_once()
            kwargs = m.call_args.kwargs
            assert kwargs["task_id"] == "t1"
            assert kwargs["next_action"] == "chase vendor for pricing"
            assert kwargs["agent"] == "email-responder"

    def test_apply_plan_ask_sets_question(self):
        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t1",
            action="ask",
            next_action=None,
            next_action_agent=None,
            question_for_operator="Drop DrFirst outreach? y/n",
            rationale="3 follow-ups ignored",
        )
        with patch("robothor.crm.dal.set_question") as m:
            m.return_value = True
            apply_plan(plan, tenant_id="default")
            m.assert_called_once()
            kwargs = m.call_args.kwargs
            assert kwargs["task_id"] == "t1"
            assert kwargs["question"] == "Drop DrFirst outreach? y/n"


class TestDryRun:
    def test_apply_plan_dry_run_skips_db_writes(self):
        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan_exec = PlanResult(
            task_id="t1",
            action="execute",
            next_action="do thing",
            next_action_agent="email-responder",
            question_for_operator=None,
            rationale="why",
        )
        plan_ask = PlanResult(
            task_id="t2",
            action="ask",
            next_action=None,
            next_action_agent=None,
            question_for_operator="decide?",
            rationale="why",
        )
        with (
            patch("robothor.crm.dal.set_next_action") as sna,
            patch("robothor.crm.dal.set_question") as sq,
        ):
            assert apply_plan(plan_exec, dry_run=True) is True
            assert apply_plan(plan_ask, dry_run=True) is True
            sna.assert_not_called()
            sq.assert_not_called()

    def test_plan_all_stalled_dry_run_bypasses_flag(self):
        """dry_run=True works even without ROBOTHOR_PLANNER_ENABLED — safe for
        smoke tests and debugging."""
        from robothor.engine.thread_planner import plan_all_stalled

        os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
        with patch(
            "robothor.engine.thread_planner._load_planner_candidates",
            return_value=[],
        ) as m:
            plan_all_stalled(tenant_id="default", dry_run=True)
            m.assert_called_once()


class TestPlannerHookFlag:
    def test_planner_disabled_by_default(self):
        """Without ROBOTHOR_PLANNER_ENABLED=1, plan_all_stalled is a no-op."""
        from robothor.engine.thread_planner import plan_all_stalled

        os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
        with patch("robothor.engine.thread_planner._load_planner_candidates") as m:
            result = plan_all_stalled(tenant_id="default")
            m.assert_not_called()
            assert result == []

    def test_planner_enabled_when_flag_set(self):
        from robothor.engine.thread_planner import plan_all_stalled

        os.environ["ROBOTHOR_PLANNER_ENABLED"] = "1"
        try:
            with patch(
                "robothor.engine.thread_planner._load_planner_candidates",
                return_value=[],
            ) as m:
                plan_all_stalled(tenant_id="default")
                m.assert_called_once()
        finally:
            os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
