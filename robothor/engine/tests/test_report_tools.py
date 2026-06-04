"""Tests for report rendering tool handlers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")

SAMPLE_DATA = {
    "period": "Week of April 7, 2026",
    "executive_summary": {
        "tickets_resolved": 86,
        "prs_merged": 335,
        "pr_to_ticket_ratio": "3.9",
        "open_backlog": "394+",
        "throughput_rate": "20 tickets/week | 78 PRs/week",
    },
    "jira": {
        "resolved": [
            {
                "project": "ENG",
                "count": 82,
                "types": "45 Tasks, 21 Bugs, 12 Stories",
                "top_contributors": "Alice (18), Bob (18), Charlie (14)",
            },
        ],
        "stale_tickets": [
            {
                "key": "ENG-51",
                "status": "To Do",
                "assignee": "Unassigned",
                "summary": "Add dashboard metrics endpoint",
            },
        ],
    },
    "github": {
        "repo_stats": [
            {
                "name": "acme/repo-alpha",
                "merged": 188,
                "avg_cycle": "2.7h",
                "avg_cycle_hours": 2.7,
                "median_cycle": "0.0h",
            },
            {
                "name": "acme/repo-beta",
                "merged": 22,
                "avg_cycle": "95.8h",
                "avg_cycle_hours": 95.8,
                "median_cycle": "1.6h",
            },
        ],
        "total_merged": 335,
        "review_coverage": 11.0,
        "total_reviews": 37,
        "no_review_repos": ["acme/repo-beta", "acme/repo-gamma"],
    },
    "people": [
        {"name": "Alice Smith", "tickets": 1, "prs": 97, "reviews": 0, "pr_per_ticket": "97.0"},
        {"name": "Bob Johnson", "tickets": 19, "prs": 10, "reviews": 28, "pr_per_ticket": "0.5"},
    ],
    "bottlenecks": [
        {
            "severity": "high",
            "text": "Only 11% of PRs receive any code review",
            "recommendation": "Require 1 reviewer on all repos",
        },
        {
            "severity": "medium",
            "text": "acme/repo-beta has 95.8h avg cycle time",
            "recommendation": "Investigate what's blocking merges",
        },
    ],
}


class TestReportToolSchemas:
    def test_tool_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            assert "render_devops_report" in registry._schemas


class TestRenderDevopsReport:
    @pytest.mark.asyncio
    async def test_missing_data(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({}, _CTX)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_render_success(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": SAMPLE_DATA}, _CTX)

        assert "html" in result
        assert "subject" in result
        assert "plain_summary" in result
        assert "Dev Team Operations Report" in result["subject"]
        assert "April 7" in result["subject"]

    @pytest.mark.asyncio
    async def test_html_contains_sections(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": SAMPLE_DATA}, _CTX)
        html = result["html"]

        assert "Executive Summary" in html
        assert "86" in html  # tickets resolved
        assert "335" in html  # prs merged
        assert "acme/repo-alpha" in html
        assert "acme/repo-beta" in html
        assert "Alice Smith" in html
        assert "11%" in html  # review coverage
        assert "Bottlenecks" in html

    @pytest.mark.asyncio
    async def test_plain_summary(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": SAMPLE_DATA}, _CTX)
        plain = result["plain_summary"]

        assert "86" in plain
        assert "335" in plain
        assert "Bottlenecks:" in plain

    @pytest.mark.asyncio
    async def test_data_quality_panel_hidden_when_clean(self):
        """Report Quality panel must not appear when there are no anomalies."""
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": SAMPLE_DATA}, _CTX)
        # Sentinel string from the rendered <h2> only appears when the block fires
        assert "unresolved handle(s)" not in result["html"]
        assert "engineer(s) on roster" not in result["html"]

    @pytest.mark.asyncio
    async def test_data_quality_panel_shows_when_unresolved(self):
        """Report Quality panel must surface unresolved handles."""
        from robothor.engine.tools.handlers.reports import _render_devops_report

        data = dict(SAMPLE_DATA)
        data["data_quality"] = {
            "unresolved_handles": [
                {
                    "channel": "github",
                    "identifier": "newhire-dev",
                    "occurrences": 4,
                    "sources": ["impetus-one/pr_stats_current_week/authors"],
                }
            ],
            "missing_from_roster": [],
        }
        result = await _render_devops_report({"report_data": data}, _CTX)
        assert "newhire-dev" in result["html"]
        assert "unresolved handle" in result["html"]

    @pytest.mark.asyncio
    async def test_data_quality_panel_shows_missing_roster(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        data = dict(SAMPLE_DATA)
        data["data_quality"] = {
            "unresolved_handles": [],
            "missing_from_roster": [
                {
                    "person_id": "p2",
                    "name": "Bob Smith",
                    "job_title": "Engineer",
                    "reason": "no activity this period",
                }
            ],
        }
        result = await _render_devops_report({"report_data": data}, _CTX)
        assert "Report Quality" in result["html"]
        assert "Bob Smith" in result["html"]

    @pytest.mark.asyncio
    async def test_accepts_json_string(self):
        import json

        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": json.dumps(SAMPLE_DATA)}, _CTX)
        assert "html" in result
        assert "Executive Summary" in result["html"]

    @pytest.mark.asyncio
    async def test_invalid_json_string(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": "not valid json"}, _CTX)
        assert "error" in result
