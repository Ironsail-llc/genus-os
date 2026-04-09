"""Tests for managed_agents.outcomes — outcome interpretation."""

from robothor.engine.managed_agents.outcomes import build_outcome_event, interpret_outcome


class TestInterpretOutcome:
    def test_satisfied(self):
        event = {"result": "satisfied", "explanation": "All criteria met", "iteration": 2}
        r = interpret_outcome(event)
        assert r["passed"] is True
        assert r["confidence"] == 1.0
        assert r["result"] == "satisfied"
        assert r["explanation"] == "All criteria met"
        assert r["iteration"] == 2

    def test_needs_revision(self):
        r = interpret_outcome({"result": "needs_revision", "explanation": "Missing tests"})
        assert r["passed"] is False
        assert r["confidence"] == 0.5

    def test_max_iterations_reached(self):
        r = interpret_outcome({"result": "max_iterations_reached"})
        assert r["passed"] is False
        assert r["confidence"] == 0.3

    def test_failed(self):
        r = interpret_outcome({"result": "failed"})
        assert r["passed"] is False
        assert r["confidence"] == 0.0

    def test_interrupted(self):
        r = interpret_outcome({"result": "interrupted"})
        assert r["passed"] is False
        assert r["confidence"] == 0.0

    def test_unknown_result(self):
        r = interpret_outcome({"result": "unknown_value"})
        assert r["passed"] is False
        assert r["confidence"] == 0.0

    def test_empty_event(self):
        r = interpret_outcome({})
        assert r["passed"] is False
        assert r["result"] == "failed"
        assert r["iteration"] == 0


class TestBuildOutcomeEvent:
    def test_basic(self):
        event = build_outcome_event("Build a report", "## Quality\n- Accurate data")
        assert event["type"] == "user.define_outcome"
        assert event["description"] == "Build a report"
        assert event["rubric"]["type"] == "text"
        assert "Accurate data" in event["rubric"]["content"]
        assert event["max_iterations"] == 5

    def test_max_iterations_clamped(self):
        event = build_outcome_event("x", "y", max_iterations=0)
        assert event["max_iterations"] == 1

        event = build_outcome_event("x", "y", max_iterations=50)
        assert event["max_iterations"] == 20

    def test_custom_iterations(self):
        event = build_outcome_event("x", "y", max_iterations=10)
        assert event["max_iterations"] == 10
