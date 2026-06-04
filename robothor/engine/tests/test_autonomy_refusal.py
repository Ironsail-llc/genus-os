"""Autonomy-budget refusal routes the planner to ``ask`` instead of executing.

These are pure unit tests (no DB) — they exercise ``classify_action`` and
``plan_thread`` directly. They live here rather than under ``tests/integration/``
so the default CI gate (``-m "not integration"``) actually runs them; the
integration directory carries an autouse DB fixture that would otherwise gate
them behind a live PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


class TestAutonomyBudgetEnforcement:
    def test_refuse_verdict_overrides_default(self):
        """A task-scoped `categories.{action_type}: refuse` overrides the default."""
        from robothor.engine.autonomy import classify_action

        budget = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 0,
            "categories": {"vendor_data_ask": "refuse"},
            "hard_floor": [],
        }
        verdict = classify_action(
            "vendor_data_ask",
            metadata={"reversible": True, "estimated_cost_usd": 0},
            budget=budget,
        )
        assert verdict == "refuse"

    def test_plan_thread_routes_refused_action_to_ask(self):
        """When the autonomy classifier refuses, the planner asks the operator."""
        from robothor.engine.thread_planner import plan_thread
        from robothor.engine.thread_pool import Thread

        thread = Thread(
            id="t-1",
            title="DrFirst",
            status="TODO",
            priority="normal",
            age_days=10,
            stale_days=3,
            requires_human=False,
            sla_breached=False,
            escalation_count=0,
            open_children=0,
            total_children=0,
            assigned_to_agent="email-responder",
        )
        history: list[dict[str, Any]] = [
            {
                "metadata": {"kind": "email_sent"},
                "created_at": datetime.now(UTC) - timedelta(hours=72),
            },
        ]
        budget = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 0,
            "categories": {"vendor_data_ask": "refuse"},
            "hard_floor": [],
        }
        plan = plan_thread(
            thread=thread,
            body="from: alice@example.com\nWaiting on pricing.",
            history=history,
            autonomy=budget,
            objective="Get pricing",
            question_for_operator=None,
            next_action=None,
            last_planned_at=None,
        )
        # Refused → planner must escalate instead of executing.
        assert plan.action == "ask"
        assert plan.question_for_operator
