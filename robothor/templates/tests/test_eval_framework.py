"""Tests for eval_framework — test case generation, grading, and transcript review."""

from robothor.templates.eval_framework import (
    TestCase,
    build_criteria_for_test_case,
    generate_test_cases,
    grade_run,
    review_transcripts,
)


class TestGenerateTestCases:
    def test_task_protocol_generates_case(self):
        manifest = {"id": "worker", "task_protocol": True}
        cases = generate_test_cases(manifest)
        assert any("task-protocol" in c.id for c in cases)
        task_case = next(c for c in cases if "task-protocol" in c.id)
        assert task_case.expected_outputs["calls_list_my_tasks"] is True
        assert task_case.expected_outputs["calls_resolve_task"] is True

    def test_status_file_generates_case(self):
        manifest = {"id": "monitor", "status_file": "brain/memory/monitor-status.md"}
        cases = generate_test_cases(manifest)
        assert any("status-file" in c.id for c in cases)

    def test_hooks_generate_cases(self):
        manifest = {
            "id": "handler",
            "hooks": [
                {"stream": "email", "event_type": "email.new", "message": "New email!"},
                {"stream": "calendar", "event_type": "calendar.new", "message": "New event!"},
            ],
        }
        cases = generate_test_cases(manifest)
        hook_cases = [c for c in cases if "hook" in c.id]
        assert len(hook_cases) == 2
        assert hook_cases[0].prompt == "New email!"

    def test_creates_tasks_generates_case(self):
        manifest = {"id": "router", "creates_tasks_for": ["responder"]}
        cases = generate_test_cases(manifest)
        assert any("creates-tasks" in c.id for c in cases)

    def test_empty_manifest_generates_no_cases(self):
        manifest = {"id": "bare"}
        cases = generate_test_cases(manifest)
        assert len(cases) == 0

    def test_all_cases_have_cost_cap(self):
        manifest = {
            "id": "full",
            "task_protocol": True,
            "status_file": "brain/memory/status.md",
            "hooks": [{"stream": "x", "event_type": "x.y", "message": "test"}],
        }
        cases = generate_test_cases(manifest)
        for case in cases:
            assert case.agent_config_overrides.get("v2", {}).get("cost_budget_usd") == 0.50


class TestBuildCriteria:
    def test_builds_from_expected_outputs(self):
        tc = TestCase(
            id="test",
            expected_outputs={
                "calls_list_my_tasks": True,
                "calls_resolve_task": True,
                "completes_without_error": True,
            },
        )
        criteria = build_criteria_for_test_case(tc)
        names = [c.name for c in criteria]
        assert "calls_list_my_tasks" in names
        assert "calls_resolve_task" in names
        assert "completes_without_error" in names


class TestGradeRun:
    def test_all_pass(self):
        run = {
            "id": "run-1",
            "status": "completed",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_cost_usd": 0.01,
            "duration_ms": 5000,
        }
        steps = [
            {
                "step_type": "tool_call",
                "tool_name": "list_my_tasks",
                "tool_input": None,
                "error_message": None,
            },
            {
                "step_type": "tool_call",
                "tool_name": "resolve_task",
                "tool_input": None,
                "error_message": None,
            },
        ]
        tc = TestCase(
            id="test", expected_outputs={"calls_list_my_tasks": True, "calls_resolve_task": True}
        )
        criteria = build_criteria_for_test_case(tc)
        result = grade_run(run, steps, criteria)
        assert result.score == 1.0
        assert len(result.passed_criteria) == 2
        assert result.tokens == 150

    def test_partial_pass(self):
        run = {
            "id": "run-2",
            "status": "completed",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_cost_usd": 0,
            "duration_ms": 0,
        }
        steps = [
            {
                "step_type": "tool_call",
                "tool_name": "list_my_tasks",
                "tool_input": None,
                "error_message": None,
            },
        ]
        tc = TestCase(
            id="test", expected_outputs={"calls_list_my_tasks": True, "calls_resolve_task": True}
        )
        criteria = build_criteria_for_test_case(tc)
        result = grade_run(run, steps, criteria)
        assert result.score == 0.5
        assert len(result.failed_criteria) == 1

    def test_no_criteria_returns_zero(self):
        run = {"id": "run-3"}
        result = grade_run(run, [], [])
        assert result.score == 0.0

    def test_error_check(self):
        run = {"id": "run-4", "status": "completed", "error_message": None}
        tc = TestCase(id="test", expected_outputs={"completes_without_error": True})
        criteria = build_criteria_for_test_case(tc)
        result = grade_run(run, [], criteria)
        assert "completes_without_error" in result.passed_criteria

    def test_error_check_fails_on_error(self):
        run = {"id": "run-5", "status": "failed", "error_message": "boom"}
        tc = TestCase(id="test", expected_outputs={"completes_without_error": True})
        criteria = build_criteria_for_test_case(tc)
        result = grade_run(run, [], criteria)
        assert "completes_without_error" in result.failed_criteria


class TestReviewTranscripts:
    def test_detects_repeated_tool_calls(self):
        runs = [{"id": "r1", "agent_id": "agent-a"}]
        steps_by_run = {
            "r1": [
                {
                    "step_type": "tool_call",
                    "tool_name": "read_file",
                    "tool_input": '{"path": "a.txt"}',
                    "error_message": None,
                },
                {
                    "step_type": "tool_call",
                    "tool_name": "read_file",
                    "tool_input": '{"path": "a.txt"}',
                    "error_message": None,
                },
            ]
        }
        insights = review_transcripts(runs, steps_by_run)
        assert any("Repeated" in i.pattern for i in insights)

    def test_detects_error_retry_loops(self):
        runs = [{"id": "r1", "agent_id": "agent-b"}]
        steps_by_run = {
            "r1": [
                {
                    "step_type": "tool_call",
                    "tool_name": "web_fetch",
                    "tool_input": None,
                    "error_message": "timeout",
                },
                {
                    "step_type": "tool_call",
                    "tool_name": "web_fetch",
                    "tool_input": None,
                    "error_message": None,
                },
            ]
        }
        insights = review_transcripts(runs, steps_by_run)
        assert any("retry" in i.pattern.lower() for i in insights)

    def test_detects_budget_exhaustion(self):
        runs = [
            {"id": "r1", "agent_id": "agent-c", "budget_exhausted": True, "total_cost_usd": 1.0},
        ]
        insights = review_transcripts(runs, {})
        assert any("budget" in i.pattern.lower() or "limit" in i.pattern.lower() for i in insights)

    def test_detects_cost_outliers(self):
        runs = [{"id": f"r{i}", "agent_id": "agent-d", "total_cost_usd": 0.01} for i in range(10)]
        # Add one outlier
        runs.append({"id": "r-outlier", "agent_id": "agent-d", "total_cost_usd": 5.0})
        insights = review_transcripts(runs, {})
        assert any("outlier" in i.pattern.lower() for i in insights)

    def test_no_insights_for_clean_runs(self):
        runs = [
            {"id": "r1", "agent_id": "agent-e", "status": "completed", "total_cost_usd": 0.01},
        ]
        steps_by_run = {
            "r1": [
                {
                    "step_type": "tool_call",
                    "tool_name": "read_file",
                    "tool_input": '{"path": "a.txt"}',
                    "error_message": None,
                },
                {
                    "step_type": "tool_call",
                    "tool_name": "write_file",
                    "tool_input": '{"path": "b.txt"}',
                    "error_message": None,
                },
            ]
        }
        insights = review_transcripts(runs, steps_by_run)
        assert len(insights) == 0
