"""Tests for difficulty-aware routing."""

from __future__ import annotations

from robothor.engine.router import classify_difficulty, get_route_config


class TestClassifyDifficulty:
    def test_manual_override(self):
        assert classify_difficulty("short msg", 5, manual_override="complex") == "complex"

    def test_plan_difficulty(self):
        assert classify_difficulty("short msg", 5, plan_difficulty="complex") == "complex"

    def test_manual_override_takes_priority(self):
        assert (
            classify_difficulty("short", 5, manual_override="simple", plan_difficulty="complex")
            == "simple"
        )

    def test_heuristic_simple(self):
        assert classify_difficulty("do it", 3) == "simple"

    def test_heuristic_moderate(self):
        msg = "x" * 200
        assert classify_difficulty(msg, 10) == "moderate"

    def test_heuristic_complex_by_length(self):
        msg = "x" * 600
        assert classify_difficulty(msg, 10) == "complex"

    def test_heuristic_complex_by_tools(self):
        assert classify_difficulty("short", 25) == "complex"


class TestGetRouteConfig:
    def test_simple_preset(self):
        route = get_route_config("hi", 3, manual_override="simple")
        assert route.difficulty == "simple"
        assert route.max_iterations_override == 5
        assert route.planning is False
        assert route.verification is False

    def test_moderate_preset(self):
        route = get_route_config("hi", 3, manual_override="moderate")
        assert route.difficulty == "moderate"
        assert route.max_iterations_override is None

    def test_complex_preset(self):
        route = get_route_config("hi", 3, manual_override="complex")
        assert route.difficulty == "complex"
        assert route.planning is True
        assert route.verification is True
        assert route.checkpoint is True
        assert route.scratchpad is True

    def test_heuristic_routing(self):
        route = get_route_config("short task", 2)
        assert route.difficulty == "simple"
