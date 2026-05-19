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
from unittest.mock import MagicMock, patch

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
    """Phase 2 flip: planner runs by default. ROBOTHOR_PLANNER_ENABLED=0 opts out.

    Why: the planner was originally guarded off-by-default to avoid surprises
    during initial rollout. Phase 2 makes it on-by-default because the canonical
    multi-day workflow (quote → PO) requires it. The env var is still respected
    as a kill switch for operators who want it off.
    """

    def test_planner_runs_by_default_when_env_unset(self):
        """With ROBOTHOR_PLANNER_ENABLED unset, plan_all_stalled fetches candidates."""
        from robothor.engine.thread_planner import plan_all_stalled

        os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
        with patch(
            "robothor.engine.thread_planner._load_planner_candidates",
            return_value=[],
        ) as m:
            result = plan_all_stalled(tenant_id="default")
            m.assert_called_once()
            assert result == []

    def test_planner_skipped_when_env_zero(self):
        """ROBOTHOR_PLANNER_ENABLED=0 is the explicit opt-out path."""
        from robothor.engine.thread_planner import plan_all_stalled

        os.environ["ROBOTHOR_PLANNER_ENABLED"] = "0"
        try:
            with patch("robothor.engine.thread_planner._load_planner_candidates") as m:
                result = plan_all_stalled(tenant_id="default")
                m.assert_not_called()
                assert result == []
        finally:
            os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)

    def test_planner_enabled_when_flag_set(self):
        """ROBOTHOR_PLANNER_ENABLED=1 is still respected (no change from old behavior)."""
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


class TestPlannerObservability:
    """Phase 2 adds structured logs + Prometheus counters to the planner.

    Why: today the planner runs silently. When it makes a bad call, there's
    no way to count `ask`s vs `execute`s, no way to time a beat, and no
    structured event for the dashboards. Phase 2 instruments it.

    All instrumentation is wrapped in suppress(Exception) so observability
    cannot break the lifecycle.
    """

    def test_apply_plan_increments_action_metric(self):
        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t1",
            action="execute",
            next_action="chase vendor",
            next_action_agent="email-responder",
            question_for_operator=None,
            rationale="48h since last reply",
        )
        with (
            patch("robothor.crm.dal.set_next_action", return_value=True),
            patch("robothor.engine.metrics.PLANNER_ACTIONS_TOTAL") as metric,
        ):
            apply_plan(plan, tenant_id="default")
            metric.labels.assert_called_with(action="execute", tenant="default")
            metric.labels.return_value.inc.assert_called_once()

    def test_apply_plan_increments_ask_metric(self):
        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t1",
            action="ask",
            next_action=None,
            next_action_agent=None,
            question_for_operator="Drop vendor?",
            rationale="3 unanswered chases",
        )
        with (
            patch("robothor.crm.dal.set_question", return_value=True),
            patch("robothor.engine.metrics.PLANNER_ACTIONS_TOTAL") as metric,
        ):
            apply_plan(plan, tenant_id="default")
            metric.labels.assert_called_with(action="ask", tenant="default")

    def test_metric_failure_does_not_break_apply_plan(self):
        """Observability outages must not break planner application.

        The production call path is ``PLANNER_ACTIONS_TOTAL.labels(...).inc()``,
        so the failure mode we have to pin is `.labels()` raising — that is
        what a Prometheus-client outage actually looks like. Patching the
        module-level symbol with ``side_effect=RuntimeError(...)`` would only
        fire on a direct call of ``PLANNER_ACTIONS_TOTAL(...)`` and the
        ``.labels()`` chain would silently route through a child MagicMock,
        making the test pass even if ``contextlib.suppress(Exception)`` were
        removed from ``_record_action_metric``.
        """
        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t1",
            action="execute",
            next_action="do",
            next_action_agent="x",
            question_for_operator=None,
            rationale="r",
        )
        mock_metric = MagicMock()
        mock_metric.labels.side_effect = RuntimeError("metric backend down")
        with (
            patch("robothor.crm.dal.set_next_action", return_value=True) as sna,
            patch("robothor.engine.metrics.PLANNER_ACTIONS_TOTAL", mock_metric),
        ):
            # Should not raise — instrumentation is best-effort.
            apply_plan(plan, tenant_id="default")
            sna.assert_called_once()
            mock_metric.labels.assert_called_once_with(action="execute", tenant="default")

    def test_plan_all_stalled_logs_run_complete(self, caplog):
        """The planner emits a structured `planner.run_complete` log line per beat."""
        import logging

        from robothor.engine.thread_planner import plan_all_stalled

        os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
        caplog.set_level(logging.INFO, logger="robothor.engine.thread_planner")
        with patch(
            "robothor.engine.thread_planner._load_planner_candidates",
            return_value=[],
        ):
            plan_all_stalled(tenant_id="default")

        run_complete = [
            r for r in caplog.records if getattr(r, "event", "") == "planner.run_complete"
        ]
        assert run_complete, "expected a planner.run_complete log record"
        record = run_complete[0]
        assert getattr(record, "tenant_id", None) == "default"
        assert hasattr(record, "candidates_count")
        assert hasattr(record, "elapsed_ms")

    def test_apply_plan_emits_planner_action_refused_warning(self, caplog):
        """When the autonomy classifier refuses, ``apply_plan`` MUST emit a
        ``planner.action.refused`` WARNING carrying ``task_id``, ``rationale``,
        and ``action_type``. This is the documented signal that dashboards
        alert on; the table in docs/PLANNER_OBSERVABILITY.md promises it.
        """
        import logging

        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t-refuse",
            action="ask",
            next_action=None,
            next_action_agent=None,
            question_for_operator="autonomy budget refused chase — what next?",
            rationale="autonomy_refuse",
            action_type="vendor_data_ask",
        )
        caplog.set_level(logging.WARNING, logger="robothor.engine.thread_planner")
        with patch("robothor.crm.dal.set_question", return_value=True):
            apply_plan(plan, tenant_id="default")

        refused = [r for r in caplog.records if getattr(r, "event", "") == "planner.action.refused"]
        assert refused, "expected a planner.action.refused log record"
        rec = refused[0]
        assert rec.levelno == logging.WARNING
        assert getattr(rec, "task_id", None) == "t-refuse"
        assert getattr(rec, "rationale", None) == "autonomy_refuse"
        assert getattr(rec, "action_type", None) == "vendor_data_ask"

    def test_plan_all_stalled_emits_run_complete_on_candidate_load_failure(self, caplog):
        """A DB outage that prevents loading candidates must still emit one
        ``planner.run_complete`` event with ``candidates_count=0``,
        ``actions={}``, and ``error=repr(exception)``. Without this, Grafana
        cannot distinguish "planner running but no work" from "planner
        crashed during candidate load" — both look like silence.
        """
        import logging

        from robothor.engine.thread_planner import plan_all_stalled

        os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
        caplog.set_level(logging.INFO, logger="robothor.engine.thread_planner")
        with patch(
            "robothor.engine.thread_planner._load_planner_candidates",
            side_effect=RuntimeError("connection refused"),
        ):
            result = plan_all_stalled(tenant_id="default")

        assert result == []
        run_complete = [
            r for r in caplog.records if getattr(r, "event", "") == "planner.run_complete"
        ]
        assert run_complete, "expected planner.run_complete even on candidate-load failure"
        rec = run_complete[0]
        assert getattr(rec, "candidates_count", None) == 0
        assert getattr(rec, "actions", None) == {}
        assert "connection refused" in (getattr(rec, "error", "") or "")

    def test_plan_all_stalled_observes_duration_on_candidate_load_failure(self):
        """``PLANNER_RUN_DURATION`` must observe on every beat — a DB outage
        is still a beat that should appear on the timing histogram, otherwise
        the planner looks "disabled" instead of "broken".
        """
        from robothor.engine.thread_planner import plan_all_stalled

        os.environ.pop("ROBOTHOR_PLANNER_ENABLED", None)
        with (
            patch(
                "robothor.engine.thread_planner._load_planner_candidates",
                side_effect=RuntimeError("connection refused"),
            ),
            patch("robothor.engine.metrics.PLANNER_RUN_DURATION") as duration,
        ):
            plan_all_stalled(tenant_id="default")
            duration.labels.assert_called_with(tenant="default")
            duration.labels.return_value.observe.assert_called_once()

    def test_apply_plan_does_not_emit_refusal_for_other_rationales(self, caplog):
        """The refusal warning fires *only* on ``rationale='autonomy_refuse'``.
        Other ask-path rationales (objective veto, no_pattern_matched) are
        normal escalations, not classifier refusals — they shouldn't pollute
        the refusal signal.
        """
        import logging

        from robothor.engine.thread_planner import PlanResult, apply_plan

        plan = PlanResult(
            task_id="t-veto",
            action="ask",
            next_action=None,
            next_action_agent=None,
            question_for_operator="Drop vendor?",
            rationale="objective_veto_meeting_repeated",
        )
        caplog.set_level(logging.WARNING, logger="robothor.engine.thread_planner")
        with patch("robothor.crm.dal.set_question", return_value=True):
            apply_plan(plan, tenant_id="default")

        refused = [r for r in caplog.records if getattr(r, "event", "") == "planner.action.refused"]
        assert refused == [], "objective_veto must not emit planner.action.refused"
