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


class TestValidateBudget:
    """The Phase-1 validator that the DAL calls before writing autonomy_budget.

    Why: today the JSONB column accepts any shape silently — a typo in
    `categories` or a stray top-level key would degrade the planner with no
    feedback. The validator rejects clearly broken inputs and lets the DAL
    return ``{"error": reason}`` per existing convention. Empty dicts and
    partial-but-recognized shapes stay valid (legacy rows must not break).
    """

    def test_empty_dict_is_valid(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({})
        assert ok is True
        assert reason == ""

    def test_default_budget_is_valid(self):
        """The platform default must round-trip cleanly."""
        from robothor.engine.autonomy import _default_budget, validate_budget

        ok, reason = validate_budget(_default_budget())
        assert ok is True, reason

    def test_rejects_non_dict(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget("not a dict")  # type: ignore[arg-type]
        assert ok is False
        assert "dict" in reason.lower()

    def test_rejects_negative_reversible_cap(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"reversible_cap_usd": -1})
        assert ok is False
        assert "reversible_cap_usd" in reason

    def test_rejects_negative_irreversible_cap(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"irreversible_cap_usd": -100})
        assert ok is False
        assert "irreversible_cap_usd" in reason

    def test_rejects_non_numeric_cap(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"reversible_cap_usd": "lots"})
        assert ok is False
        assert "reversible_cap_usd" in reason

    def test_rejects_unknown_category_verdict(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"categories": {"vendor_data_ask": "maybe"}})
        assert ok is False
        assert "maybe" in reason or "category" in reason.lower()

    def test_rejects_non_dict_categories(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"categories": ["auto"]})
        assert ok is False
        assert "categories" in reason

    def test_rejects_non_list_hard_floor(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"hard_floor": "deletes_data"})
        assert ok is False
        assert "hard_floor" in reason

    def test_rejects_non_string_hard_floor_entry(self):
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"hard_floor": ["deletes_data", 42]})
        assert ok is False
        assert "hard_floor" in reason

    def test_rejects_extra_top_level_keys(self):
        """Unknown top-level keys are typos, not features."""
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"reversible_cap_usd": 100, "spend_cap": 1000})
        assert ok is False
        assert "spend_cap" in reason

    def test_accepts_partial_shape(self):
        """Legacy rows may carry only a subset of keys — must stay valid."""
        from robothor.engine.autonomy import validate_budget

        ok, reason = validate_budget({"categories": {"vendor_data_ask": "auto"}})
        assert ok is True, reason

    def test_accepts_all_three_verdicts(self):
        from robothor.engine.autonomy import validate_budget

        budget = {
            "categories": {
                "vendor_data_ask": "auto",
                "calendar_send_new": "ask",
                "deletes_data": "refuse",
            }
        }
        ok, reason = validate_budget(budget)
        assert ok is True, reason
