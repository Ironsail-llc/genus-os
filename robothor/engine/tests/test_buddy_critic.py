"""Tests for the Buddy Critic review engine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.buddy_critic import (
    Evidence,
    Finding,
    Review,
    _extract_json,
    aggregate_findings,
    build_evidence,
    open_task_for_finding,
    persist_review,
    review_run,
    sample_runs_to_review,
)
from robothor.engine.goals import EXCLUDED_FROM_SELF_IMPROVE


def _make_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _make_evidence(**overrides) -> Evidence:
    defaults = {
        "run_id": "run-abc",
        "agent_id": "email-responder",
        "status": "failed",
        "started_at": datetime(2026, 4, 19, 10, 0, 0, tzinfo=UTC),
        "duration_ms": 62000,
        "total_cost_usd": 0.12,
        "output_text_truncated": "Timed out waiting for gws_gmail_send.",
        "error_message": "TimeoutError: gws_gmail_send exceeded 60s",
        "error_steps": [
            {
                "step_number": 4,
                "step_type": "error",
                "tool_name": "gws_gmail_send",
                "error_message": "TimeoutError",
                "duration_ms": 61000,
            }
        ],
        "tool_call_count": 5,
        "tool_error_count": 2,
    }
    defaults.update(overrides)
    return Evidence(**defaults)


class TestExtractJSON:
    def test_plain_json(self):
        assert _extract_json('{"rating": 3, "dimension": "quality"}') == {
            "rating": 3,
            "dimension": "quality",
        }

    def test_markdown_fenced(self):
        raw = '```json\n{"rating": 2}\n```'
        assert _extract_json(raw) == {"rating": 2}

    def test_leading_prose(self):
        raw = 'Here is the review:\n{"rating": 4, "dimension": "efficiency"}\nThanks!'
        assert _extract_json(raw) == {"rating": 4, "dimension": "efficiency"}

    def test_empty(self):
        assert _extract_json("") is None
        assert _extract_json("no json here") is None


class TestSampleRunsToReview:
    @patch("robothor.db.connection.get_connection")
    def test_query_includes_dedup_and_bias(self, mock_get_conn):
        cur = MagicMock()
        cur.fetchall.return_value = [("run-1",), ("run-2",)]
        mock_get_conn.return_value = _make_conn(cur)

        result = sample_runs_to_review("email-responder", n=5, hours=24)

        assert result == ["run-1", "run-2"]
        # Verify the SQL contains the dedup, status filter, and bias ordering
        executed_sql = cur.execute.call_args[0][0]
        assert "NOT EXISTS" in executed_sql
        assert "reviewer_type = 'buddy'" in executed_sql
        assert "parent_run_id IS NULL" in executed_sql
        assert "'failed'" in executed_sql  # bias toward failures
        # Regression: must exclude in-flight runs — duration_ms is null and
        # Buddy would mis-read them as "agent stalled with 0 tool calls".
        assert "status IN ('completed', 'failed', 'timeout')" in executed_sql


class TestBuildEvidence:
    @patch("robothor.db.connection.get_connection")
    def test_returns_none_for_missing_run(self, mock_get_conn):
        cur = MagicMock()
        cur.fetchone.return_value = None
        mock_get_conn.return_value = _make_conn(cur)

        assert build_evidence("missing-run") is None

    @patch("robothor.db.connection.get_connection")
    def test_truncates_long_output(self, mock_get_conn):
        long_output = "x" * 3000
        cur = MagicMock()
        cur.fetchone.side_effect = [
            ("run-x", "email-responder", "completed", None, 1000, 0.01, long_output, None),
            (5, 0),  # tool_calls, tool_errors query
        ]
        cur.fetchall.return_value = []  # no error steps
        mock_get_conn.return_value = _make_conn(cur)

        ev = build_evidence("run-x")
        assert ev is not None
        assert ev.output_text_truncated.endswith("…")
        assert len(ev.output_text_truncated) < len(long_output)


class TestReviewRun:
    @pytest.mark.asyncio
    async def test_parses_valid_llm_response(self):
        evidence = _make_evidence()
        fake_response = MagicMock()
        fake_response.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "rating": 2,
                            "dimension": "efficiency",
                            "specific_issue": "gws_gmail_send timed out after 60s",
                            "suggested_action": "Reduce stall_timeout or lower retries",
                        }
                    )
                )
            )
        ]

        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            review = await review_run(evidence)

        assert review is not None
        assert review.rating == 2
        assert review.dimension == "efficiency"
        assert "gws_gmail_send" in review.specific_issue
        assert review.agent_id == "email-responder"

    @pytest.mark.asyncio
    async def test_rejects_unknown_dimension(self):
        evidence = _make_evidence()
        fake_response = MagicMock()
        fake_response.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "rating": 3,
                            "dimension": "nonsense",
                            "specific_issue": "concrete note",
                            "suggested_action": "action",
                        }
                    )
                )
            )
        ]

        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            review = await review_run(evidence)

        # Unknown dimensions are coerced to 'correctness' rather than rejected
        assert review is not None
        assert review.dimension == "correctness"

    @pytest.mark.asyncio
    async def test_refuses_content_free_review(self):
        """Empty specific_issue = generic filler. Refuse to persist."""
        evidence = _make_evidence()
        fake_response = MagicMock()
        fake_response.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "rating": 5,
                            "dimension": "quality",
                            "specific_issue": "",
                            "suggested_action": "",
                        }
                    )
                )
            )
        ]

        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            review = await review_run(evidence)
        assert review is None

    @pytest.mark.asyncio
    async def test_handles_unparseable_response(self):
        evidence = _make_evidence()
        fake_response = MagicMock()
        fake_response.choices = [MagicMock(message=MagicMock(content="I cannot help with this."))]

        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            review = await review_run(evidence)
        assert review is None

    @pytest.mark.asyncio
    async def test_clamps_rating(self):
        evidence = _make_evidence()
        fake_response = MagicMock()
        fake_response.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "rating": 99,  # out of range — must clamp
                            "dimension": "quality",
                            "specific_issue": "specific",
                            "suggested_action": "do something",
                        }
                    )
                )
            )
        ]

        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            review = await review_run(evidence)
        assert review is not None
        assert review.rating == 5


class TestPersistReview:
    @patch("robothor.engine.buddy_critic._journal")
    @patch("robothor.crm.dal.create_review")
    def test_writes_with_buddy_reviewer_type(self, mock_create, mock_journal):
        mock_create.return_value = "rev-uuid-123"
        review = Review(
            agent_id="email-responder",
            run_id="run-42",
            rating=2,
            dimension="efficiency",
            specific_issue="timeout on gws_gmail_send",
            suggested_action="lower stall_timeout",
            raw_evidence=_make_evidence(),
        )

        review_id = persist_review(review)

        assert review_id == "rev-uuid-123"
        kwargs = mock_create.call_args.kwargs
        assert kwargs["reviewer"] == "buddy"
        assert kwargs["reviewer_type"] == "buddy"
        assert kwargs["rating"] == 2
        assert kwargs["run_id"] == "run-42"
        # Journal write happens exactly once
        mock_journal.assert_called_once()


class TestFindingTaskBody:
    def test_baseline_embedded_for_grader(self):
        finding = Finding(
            agent_id="email-responder",
            dimension="efficiency",
            metric="timeout_rate",
            severity=4.5,
            consecutive_days_breached=3,
            baseline_metric=0.22,
            target="<0.10",
            representative_run_ids=["run-1"],
            representative_feedback=["timed out on gws_gmail_send (4 times)"],
            corrective_actions=[
                "Classify timeouts by in-flight tool call.",
                "Lower stall_timeout_seconds to fail fast on wedged calls.",
            ],
        )
        body = finding.task_body()
        assert "email-responder" in body
        assert "timeout_rate" in body
        assert "<0.10" in body
        assert "0.22" in body
        assert "Classify timeouts" in body
        # Machine-readable baseline marker for the grader
        assert "buddy-baseline:" in body
        assert '"baseline": 0.22' in body

    def test_tags_contain_agent_and_metric(self):
        finding = Finding(
            agent_id="main",
            dimension="correctness",
            metric="error_rate",
            severity=3.0,
            consecutive_days_breached=3,
            baseline_metric=0.08,
            target="<0.02",
            representative_run_ids=[],
            representative_feedback=[],
            corrective_actions=[],
        )
        tags = finding.task_tags()
        assert "nightwatch" in tags
        assert "self-improve" in tags
        assert "main" in tags
        assert "error_rate" in tags


class TestOpenTaskForFinding:
    @patch("robothor.engine.buddy_critic._journal")
    @patch("robothor.crm.dal.create_task")
    @patch("robothor.crm.dal.list_tasks")
    def test_creates_task_when_no_duplicate(self, mock_list, mock_create, mock_journal):
        mock_list.return_value = []  # no existing open tasks
        mock_create.return_value = "task-new-1"
        finding = Finding(
            agent_id="main",
            dimension="correctness",
            metric="error_rate",
            severity=3.0,
            consecutive_days_breached=3,
            baseline_metric=0.08,
            target="<0.02",
            representative_run_ids=[],
            representative_feedback=[],
            corrective_actions=[],
        )

        task_id = open_task_for_finding(finding)

        assert task_id == "task-new-1"
        kwargs = mock_create.call_args.kwargs
        assert kwargs["assigned_to_agent"] == "auto-agent"
        assert kwargs["created_by_agent"] == "buddy"
        assert "self-improve" in kwargs["tags"]
        mock_journal.assert_called_once()

    @patch("robothor.engine.buddy_critic._journal")
    @patch("robothor.crm.dal.create_task")
    @patch("robothor.crm.dal.list_tasks")
    def test_dedups_when_open_task_exists(self, mock_list, mock_create, mock_journal):
        mock_list.return_value = [{"id": "existing-task", "status": "IN_PROGRESS"}]
        finding = Finding(
            agent_id="main",
            dimension="correctness",
            metric="error_rate",
            severity=5.0,
            consecutive_days_breached=5,
            baseline_metric=0.15,
            target="<0.02",
            representative_run_ids=[],
            representative_feedback=[],
            corrective_actions=[],
        )

        task_id = open_task_for_finding(finding)

        assert task_id is None
        mock_create.assert_not_called()
        mock_journal.assert_not_called()

    @patch("robothor.engine.buddy_critic._journal")
    @patch("robothor.crm.dal.create_task")
    @patch("robothor.crm.dal.list_tasks")
    def test_reopens_when_previous_task_is_done(self, mock_list, mock_create, mock_journal):
        """A DONE task doesn't block a new finding — the grader handles that path."""
        mock_list.return_value = [{"id": "old-done", "status": "DONE"}]
        mock_create.return_value = "task-reopen"
        finding = Finding(
            agent_id="x",
            dimension="correctness",
            metric="error_rate",
            severity=3.0,
            consecutive_days_breached=3,
            baseline_metric=0.1,
            target="<0.02",
            representative_run_ids=[],
            representative_feedback=[],
            corrective_actions=[],
        )
        task_id = open_task_for_finding(finding)
        assert task_id == "task-reopen"

    @patch("robothor.engine.buddy_critic._journal")
    @patch("robothor.crm.dal.create_task")
    @patch("robothor.crm.dal.list_tasks")
    def test_refuses_to_open_task_on_meta_agent(self, mock_list, mock_create, mock_journal):
        """Belt-and-suspenders: even if an excluded agent slips through aggregation,
        open_task_for_finding refuses. auto-agent must never be asked to optimize
        itself or its supervisors — that would let the loop silently edit its own
        guardrails."""
        mock_list.return_value = []
        finding = Finding(
            agent_id="buddy",  # a meta-agent
            dimension="correctness",
            metric="error_rate",
            severity=5.0,
            consecutive_days_breached=5,
            baseline_metric=0.15,
            target="<0.05",
            representative_run_ids=[],
            representative_feedback=[],
            corrective_actions=[],
        )
        task_id = open_task_for_finding(finding)
        assert task_id is None
        mock_create.assert_not_called()


class TestMetaAgentExclusion:
    def test_excluded_set_covers_self_improvement_loop(self):
        """The loop's own agents must never file tasks on themselves.
        If this set changes, update docs/agents/GOAL_TAXONOMY.md too."""
        assert "buddy" in EXCLUDED_FROM_SELF_IMPROVE
        assert "buddy-grader" in EXCLUDED_FROM_SELF_IMPROVE
        assert "buddy-auditor" in EXCLUDED_FROM_SELF_IMPROVE
        assert "auto-agent" in EXCLUDED_FROM_SELF_IMPROVE
        assert "auto-researcher" in EXCLUDED_FROM_SELF_IMPROVE


class TestReviewModelFromManifest:
    """Fix 5 — Buddy's review model must read from buddy.yaml, not from a
    hardcoded constant. Root CLAUDE.md rule 6: manifests are source of
    truth for models. A broken manifest still falls back safely."""

    def setup_method(self):
        # Reset the module cache before each test so the mtime check refires.
        import robothor.engine.buddy_critic as bc

        bc._review_model_cache = None

    def test_reads_model_primary_from_manifest(self):
        from robothor.engine.buddy_critic import _get_review_model
        from robothor.engine.models import AgentConfig

        fake_config = AgentConfig(
            id="buddy",
            name="Buddy",
            model_primary="openrouter/fancy/new-model",
        )
        with patch(
            "robothor.engine.config.load_agent_config",
            return_value=fake_config,
        ):
            model = _get_review_model()
        assert model == "openrouter/fancy/new-model"

    def test_fancy_is_cached_between_calls(self):
        """Back-to-back calls don't re-parse buddy.yaml (mtime-cached)."""
        import robothor.engine.buddy_critic as bc
        from robothor.engine.buddy_critic import _get_review_model
        from robothor.engine.models import AgentConfig

        fake_config = AgentConfig(id="buddy", name="Buddy", model_primary="openrouter/cached/x")
        with patch(
            "robothor.engine.config.load_agent_config",
            return_value=fake_config,
        ) as mock_loader:
            _get_review_model()
            _get_review_model()
            _get_review_model()
        # Loader only hit once — rest served from cache.
        assert mock_loader.call_count == 1
        bc._review_model_cache = None  # reset for next test

    def test_falls_back_when_loader_raises(self):
        from robothor.engine.buddy_critic import DEFAULT_REVIEW_MODEL, _get_review_model

        with patch(
            "robothor.engine.config.load_agent_config",
            side_effect=RuntimeError("manifest parse failed"),
        ):
            model = _get_review_model()
        assert model == DEFAULT_REVIEW_MODEL
        assert "sonnet" in model.lower()

    def test_falls_back_when_model_primary_empty(self):
        from robothor.engine.buddy_critic import DEFAULT_REVIEW_MODEL, _get_review_model
        from robothor.engine.models import AgentConfig

        fake_config = AgentConfig(id="buddy", name="Buddy", model_primary="")
        with patch(
            "robothor.engine.config.load_agent_config",
            return_value=fake_config,
        ):
            model = _get_review_model()
        assert model == DEFAULT_REVIEW_MODEL

    @pytest.mark.asyncio
    async def test_review_run_uses_manifest_model_by_default(self):
        """When review_run is called without an explicit model kwarg, it
        resolves from the manifest rather than a hardcoded constant."""
        from robothor.engine.models import AgentConfig

        fake_config = AgentConfig(
            id="buddy",
            name="Buddy",
            model_primary="openrouter/test/x",
        )
        evidence = _make_evidence()
        fake_response = MagicMock()
        fake_response.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "rating": 4,
                            "dimension": "quality",
                            "specific_issue": "specific note",
                            "suggested_action": "specific action",
                        }
                    )
                )
            )
        ]

        with (
            patch(
                "robothor.engine.config.load_agent_config",
                return_value=fake_config,
            ),
            patch(
                "robothor.engine.llm_client.llm_call",
                new_callable=AsyncMock,
                return_value=fake_response,
            ) as mock_llm,
        ):
            await review_run(evidence)

        # The model passed to llm_call matches the manifest-resolved value.
        assert mock_llm.call_args.kwargs["model"] == "openrouter/test/x"


class TestAggregateFindings:
    @patch("robothor.engine.buddy_critic._recent_buddy_reviews")
    @patch("robothor.engine.buddy_critic.detect_goal_breach")
    @patch("robothor.engine.buddy_critic.compute_goal_metrics")
    @patch("robothor.engine.buddy_critic.parse_goals_from_manifest")
    @patch("robothor.engine.buddy_critic._load_manifests")
    def test_emits_finding_only_above_threshold(
        self,
        mock_load,
        mock_parse,
        mock_compute_metrics,
        mock_detect,
        mock_reviews,
    ):
        from robothor.engine.goals import GoalBreach

        mock_load.return_value = [("email-responder", {"id": "email-responder"})]
        mock_parse.return_value = [object()]
        mock_reviews.return_value = []
        mock_compute_metrics.return_value = {"error_rate": 0.12}

        # Breach below severity threshold (weight 1.0 × 3 days = 3.0, exactly at threshold)
        # Weight 1.0 × 2 days = 2.0 — below threshold, should be filtered
        low_severity = GoalBreach(
            goal_id="low-error",
            category="correctness",
            metric="error_rate",
            target="<0.05",
            actual=0.12,
            consecutive_days_breached=2,
            weight=1.0,
        )
        high_severity = GoalBreach(
            goal_id="low-error",
            category="correctness",
            metric="error_rate",
            target="<0.05",
            actual=0.12,
            consecutive_days_breached=5,
            weight=1.5,
        )

        mock_detect.return_value = [low_severity]
        assert aggregate_findings() == []

        mock_detect.return_value = [high_severity]
        findings = aggregate_findings()
        assert len(findings) == 1
        assert findings[0].agent_id == "email-responder"
        assert findings[0].metric == "error_rate"
        assert findings[0].severity == 7.5  # 1.5 × 5
        assert findings[0].baseline_metric == 0.12

    @patch("robothor.engine.buddy_critic._recent_buddy_reviews")
    @patch("robothor.engine.buddy_critic.detect_goal_breach")
    @patch("robothor.engine.buddy_critic.compute_goal_metrics")
    @patch("robothor.engine.buddy_critic.parse_goals_from_manifest")
    @patch("robothor.engine.buddy_critic._load_manifests")
    def test_skips_meta_agents(
        self,
        mock_load,
        mock_parse,
        mock_compute_metrics,
        mock_detect,
        mock_reviews,
    ):
        """Meta-agents (buddy, grader, auditor, auto-agent, auto-researcher) are
        excluded from self-improve task creation. They can still be scored and
        reviewed — but the loop must not assign auto-agent to fix its own
        supervisors. That would let the pipeline silently edit its guardrails."""
        from robothor.engine.goals import GoalBreach

        # Load a mix of meta and non-meta agents; both have persistent breaches.
        mock_load.return_value = [
            ("buddy", {"id": "buddy"}),
            ("auto-agent", {"id": "auto-agent"}),
            ("email-responder", {"id": "email-responder"}),
        ]
        mock_parse.return_value = [object()]
        mock_reviews.return_value = []
        mock_compute_metrics.return_value = {"error_rate": 0.20}

        breach = GoalBreach(
            goal_id="low-error",
            category="correctness",
            metric="error_rate",
            target="<0.05",
            actual=0.20,
            consecutive_days_breached=5,
            weight=1.0,
        )
        mock_detect.return_value = [breach]

        findings = aggregate_findings()
        # Only the non-meta agent should produce a finding.
        assert len(findings) == 1
        assert findings[0].agent_id == "email-responder"

    @patch("robothor.engine.buddy_critic._recent_buddy_reviews")
    @patch("robothor.engine.buddy_critic.detect_goal_breach")
    @patch("robothor.engine.buddy_critic.compute_goal_metrics")
    @patch("robothor.engine.buddy_critic.parse_goals_from_manifest")
    @patch("robothor.engine.buddy_critic._load_manifests")
    def test_attaches_matching_dimension_reviews(
        self,
        mock_load,
        mock_parse,
        mock_compute_metrics,
        mock_detect,
        mock_reviews,
    ):
        from robothor.engine.goals import GoalBreach

        mock_load.return_value = [("x", {"id": "x"})]
        mock_parse.return_value = [object()]
        mock_compute_metrics.return_value = {"error_rate": 0.5}
        mock_detect.return_value = [
            GoalBreach(
                goal_id="low-error",
                category="correctness",
                metric="error_rate",
                target="<0.02",
                actual=0.5,
                consecutive_days_breached=4,
                weight=1.0,
            )
        ]
        mock_reviews.return_value = [
            {"run_id": "r1", "rating": 2, "feedback": "tool errored", "dimension": "correctness"},
            {"run_id": "r2", "rating": 5, "feedback": "fine run", "dimension": "quality"},
            {"run_id": "r3", "rating": 1, "feedback": "hard fail", "dimension": "correctness"},
        ]
        findings = aggregate_findings()
        assert len(findings) == 1
        # Representative reviews should be the two correctness ones, lowest rating first
        assert findings[0].representative_run_ids == ["r3", "r1"]
        assert "hard fail" in findings[0].representative_feedback[0]
