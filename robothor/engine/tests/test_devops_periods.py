"""Tests for ET-aligned weekly windows used by the DevOps report pipeline.

The pipeline trigger fires Monday 09:00 ET; window math must be in
America/New_York so the report's "last week" matches the team's calendar.
"""

from __future__ import annotations

from datetime import datetime

from robothor.engine.reports.devops_periods import (
    ET,
    week_windows_github,
    week_windows_jira,
)


class TestWeekWindowsGithub:
    """ET-aligned Monday boundaries, expressed as UTC RFC3339 for GitHub."""

    def test_monday_morning_et_starts_at_et_midnight(self):
        # Mon 2026-05-18 09:00 EDT (UTC-4). ET Monday 00:00 == 04:00 UTC.
        now = datetime(2026, 5, 18, 9, 0, tzinfo=ET)
        current, last_since, last_until = week_windows_github(now)
        assert current == "2026-05-18T04:00:00+00:00"
        assert last_since == "2026-05-11T04:00:00+00:00"
        assert last_until == "2026-05-18T04:00:00+00:00"

    def test_midweek_aligns_to_this_weeks_monday(self):
        now = datetime(2026, 5, 20, 14, 0, tzinfo=ET)
        current, _, _ = week_windows_github(now)
        assert current == "2026-05-18T04:00:00+00:00"

    def test_sunday_evening_et_stays_on_previous_monday(self):
        # Sun 2026-05-17 22:00 ET — calendar still belongs to the 05-11 week.
        # Critical regression case: old UTC-based code mis-bucketed these to
        # the *next* week because UTC was already Monday.
        now = datetime(2026, 5, 17, 22, 0, tzinfo=ET)
        current, _, _ = week_windows_github(now)
        assert current == "2026-05-11T04:00:00+00:00"

    def test_dst_fall_back_keeps_calendar_monday(self):
        # Mon 2026-11-02 09:00 EST (UTC-5, after fall-back).
        # Current Monday: 2026-11-02 00:00 EST == 05:00 UTC.
        # Last Monday: 2026-10-26 00:00 EDT == 04:00 UTC (different offset).
        now = datetime(2026, 11, 2, 9, 0, tzinfo=ET)
        current, last_since, last_until = week_windows_github(now)
        assert current == "2026-11-02T05:00:00+00:00"
        assert last_since == "2026-10-26T04:00:00+00:00"
        assert last_until == "2026-11-02T05:00:00+00:00"

    def test_dst_spring_forward_keeps_calendar_monday(self):
        # Mon 2026-03-09 09:00 EDT (UTC-4, after spring-forward).
        # Current Monday: 2026-03-09 00:00 EDT == 04:00 UTC.
        # Last Monday: 2026-03-02 00:00 EST == 05:00 UTC (different offset).
        now = datetime(2026, 3, 9, 9, 0, tzinfo=ET)
        current, last_since, _ = week_windows_github(now)
        assert current == "2026-03-09T04:00:00+00:00"
        assert last_since == "2026-03-02T05:00:00+00:00"


class TestWeekWindowsJira:
    """ET-aligned Monday boundaries, expressed as YYYY-MM-DD for JQL.

    JIRA evaluates bare-date JQL in the user's JIRA-account timezone; for
    exact alignment, that account TZ must be America/New_York.
    """

    def test_monday_morning_et(self):
        now = datetime(2026, 5, 18, 9, 0, tzinfo=ET)
        current, last_since, last_until = week_windows_jira(now)
        assert current == "2026-05-18"
        assert last_since == "2026-05-11"
        assert last_until == "2026-05-18"

    def test_dst_boundary(self):
        now = datetime(2026, 11, 2, 9, 0, tzinfo=ET)
        current, last_since, last_until = week_windows_jira(now)
        assert current == "2026-11-02"
        assert last_since == "2026-10-26"
        assert last_until == "2026-11-02"

    def test_sunday_evening_stays_on_previous_monday(self):
        now = datetime(2026, 5, 17, 22, 0, tzinfo=ET)
        current, _, _ = week_windows_jira(now)
        assert current == "2026-05-11"
