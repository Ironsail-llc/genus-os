"""Tests for report rendering tool handlers."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")

_PERIOD = {
    "week_start": "2026-04-06",
    "week_end": "2026-04-12",
    "report_label": "Week of 2026-04-06",
}


def _metrics(**over):
    base = {
        "tickets_resolved": 0,
        "tickets_by_type": {},
        "in_progress": 0,
        "prs_merged": 0,
        "commits": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "reviews_given": 0,
        "reviews_approved": 0,
        "reviews_changes_requested": 0,
        "reviews_commented": 0,
        "review_turnaround_median_h": None,
        "changes_requested_received": 0,
        "merged_without_review": 0,
        "stale_prs": 0,
    }
    base.update(over)
    return base


SAMPLE_DATA = {
    "period": _PERIOD,
    "roles": [{"key": "eng", "title": "Software Engineer", "focus": "Ships features"}],
    "members": [
        {
            "id": "alice",
            "name": "Alice Smith",
            "role": "eng",
            "role_title": "Software Engineer",
            "metrics": _metrics(prs_merged=97, tickets_resolved=1, commits=40),
            "unavailable": {},
            "trend": {},
            "usage": [],
            "no_activity": False,
        },
        {
            "id": "bob",
            "name": "Bob Johnson",
            "role": "eng",
            "role_title": "Software Engineer",
            "metrics": _metrics(tickets_resolved=19, reviews_given=28),
            "unavailable": {},
            "trend": {},
            "usage": [],
            "no_activity": False,
        },
    ],
    "team": {"prs_merged": 97, "commits": 40, "tickets_resolved": 20, "reviews_given": 28},
    "usage": {"available": False, "reason": "no email"},
    "data_quality": {},
    "analyst": {
        "summary": "A busy week.",
        "per_person": {"alice": "Alice merged 97 PRs."},
        "bottlenecks": [
            {"severity": "high", "text": "Only one reviewer", "recommendation": "Add one"}
        ],
    },
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
    async def test_old_analyst_shape_is_refused(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report(
            {"report_data": {"executive_summary": {}, "people": []}}, _CTX
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_render_success(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": SAMPLE_DATA}, _CTX)
        assert result["subject"] == "Dev Team Operations Report — Week of 2026-04-06"
        html = result["html"]
        assert "Alice Smith" in html
        assert "Software Engineer" in html
        assert "97" in html
        assert "Bottlenecks" in html
        assert "Unavailable: no email" in html

    @pytest.mark.asyncio
    async def test_plain_summary(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": SAMPLE_DATA}, _CTX)
        plain = result["plain_summary"]
        assert "PRs merged: 97" in plain
        assert "Bottlenecks:" in plain
        assert "'tz'" not in plain

    @pytest.mark.asyncio
    async def test_data_quality_panel_hidden_when_clean(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        data = dict(SAMPLE_DATA)
        data["usage"] = {"available": True, "rows": []}
        result = await _render_devops_report({"report_data": data}, _CTX)
        assert "Data quality</h2>" not in result["html"]

    @pytest.mark.asyncio
    async def test_data_quality_panel_shows_outside_roster(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        data = dict(SAMPLE_DATA)
        data["data_quality"] = {
            "outside_roster": [
                {"handle": "newhire-dev", "source": "github", "counts": {"prs_merged": 4}}
            ]
        }
        result = await _render_devops_report({"report_data": data}, _CTX)
        assert "newhire-dev" in result["html"]
        assert "not on the team roster" in result["html"]

    @pytest.mark.asyncio
    async def test_accepts_json_string(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": json.dumps(SAMPLE_DATA)}, _CTX)
        assert "html" in result

    @pytest.mark.asyncio
    async def test_invalid_json_string(self):
        from robothor.engine.tools.handlers.reports import _render_devops_report

        result = await _render_devops_report({"report_data": "not valid json"}, _CTX)
        assert "error" in result
