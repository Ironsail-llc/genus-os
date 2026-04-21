"""Stage 4 — autonomy classifier.

The classifier decides whether the planner can ACT on an inferred next action
or must ASK the operator. Objective vetoes (phrases like "without scheduling
a meeting") always win over numeric budgets — that's the DrFirst lesson.
"""

from __future__ import annotations


class TestClassifyAction:
    def test_reversible_under_cap_is_auto(self):
        from robothor.engine.autonomy import classify_action

        budget = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 0,
            "categories": {},
            "hard_floor": [],
        }
        verdict = classify_action(
            "vendor_data_ask",
            metadata={"reversible": True, "estimated_cost_usd": 0},
            budget=budget,
        )
        assert verdict == "auto"

    def test_irreversible_always_asks_by_default(self):
        from robothor.engine.autonomy import classify_action

        budget = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 0,
            "categories": {},
            "hard_floor": [],
        }
        verdict = classify_action(
            "contract_signature",
            metadata={"reversible": False, "estimated_cost_usd": 100},
            budget=budget,
        )
        assert verdict == "ask"

    def test_hard_floor_overrides_everything(self):
        from robothor.engine.autonomy import classify_action

        budget = {
            "reversible_cap_usd": 10000,
            "irreversible_cap_usd": 10000,
            "categories": {"pushes_to_main": "auto"},
            "hard_floor": ["pushes_to_main"],
        }
        verdict = classify_action(
            "pushes_to_main",
            metadata={"reversible": True, "estimated_cost_usd": 0},
            budget=budget,
        )
        assert verdict == "refuse"

    def test_category_override_wins_over_cost_gate(self):
        from robothor.engine.autonomy import classify_action

        # Category says "auto" even though category is irreversible by nature.
        budget = {
            "reversible_cap_usd": 0,
            "irreversible_cap_usd": 0,
            "categories": {"calendar_reply_existing": "auto"},
            "hard_floor": [],
        }
        verdict = classify_action(
            "calendar_reply_existing",
            metadata={"reversible": False, "estimated_cost_usd": 0},
            budget=budget,
        )
        assert verdict == "auto"

    def test_meeting_ask_is_refused_when_objective_vetos_it(self):
        """DrFirst scenario — objective says "without scheduling a meeting".
        Any calendar_send_new action must be refused regardless of budget."""
        from robothor.engine.autonomy import classify_action

        budget = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 500,
            "categories": {"calendar_send_new": "auto"},
            "hard_floor": [],
        }
        verdict = classify_action(
            "calendar_send_new",
            metadata={
                "reversible": True,
                "estimated_cost_usd": 0,
                "objective": ("Confirm RxHistory pricing without scheduling a meeting"),
            },
            budget=budget,
        )
        assert verdict == "refuse"

    def test_objective_veto_variants(self):
        """'async', 'by email', 'without a meeting' all veto calendar_send_new."""
        from robothor.engine.autonomy import classify_action

        budget = {
            "reversible_cap_usd": 500,
            "irreversible_cap_usd": 500,
            "categories": {"calendar_send_new": "auto"},
            "hard_floor": [],
        }
        for veto in [
            "keep this async",
            "answer by email only",
            "no meeting needed",
            "Without scheduling a meeting",
        ]:
            verdict = classify_action(
                "calendar_send_new",
                metadata={"objective": veto, "reversible": True},
                budget=budget,
            )
            assert verdict == "refuse", f"Expected refuse for objective {veto!r}"


class TestLoadTenantDefaults:
    def test_defaults_shape(self):
        """Defaults must include the four required keys."""
        from robothor.engine.autonomy import load_tenant_defaults

        d = load_tenant_defaults("nonexistent-tenant")
        assert "reversible_cap_usd" in d
        assert "irreversible_cap_usd" in d
        assert "categories" in d
        assert "hard_floor" in d
        assert isinstance(d["categories"], dict)
        assert isinstance(d["hard_floor"], list)
