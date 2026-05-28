"""Tests for the Rip 2 class-level skill-naming guardrail."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from robothor.engine.skills import class_level_check
from robothor.engine.tools.dispatch import ToolContext


class TestClassLevelCheck:
    def test_clean_class_level_name_passes(self) -> None:
        for name in (
            "database-migrations",
            "api-client-debugging",
            "memory-fallback-to-notes",
            "code-review",
            "test-driven-development",
        ):
            assert class_level_check(name) is None, f"clean name rejected: {name}"

    def test_pr_number_rejected(self) -> None:
        for name in ("alerts-pr-123", "fix-pr-456", "patch-pr-7"):
            assert class_level_check(name) is not None

    def test_issue_number_rejected(self) -> None:
        assert class_level_check("close-#42") is not None

    def test_fix_prefix_rejected(self) -> None:
        assert class_level_check("fix-alerts-typecheck") is not None
        assert class_level_check("fix-broken-runner") is not None

    def test_debug_prefix_rejected(self) -> None:
        assert class_level_check("debug-monday-incident") is not None

    def test_audit_prefix_rejected(self) -> None:
        assert class_level_check("audit-tier-3-budget") is not None

    def test_day_of_week_suffix_rejected(self) -> None:
        for name in (
            "morning-briefing-monday",
            "report-friday",
            "review-today",
            "incident-yesterday",
        ):
            assert class_level_check(name) is not None, f"date-suffix passed: {name}"

    def test_error_keyword_in_name_rejected(self) -> None:
        # 'fix-memory-error' has the error keyword AND a one-off prefix.
        # 'investigate-crash-2024-04-22' bundles both crash + a date.
        for name in ("trace-error-on-startup", "patch-exception-flow"):
            assert class_level_check(name) is not None, f"error-name passed: {name}"

    def test_error_recovery_class_passes(self) -> None:
        # 'crash-recovery' is a real class of work — fault-handling
        # patterns. The guardrail must not over-block useful names.
        for name in ("crash-recovery", "error-handling-patterns", "exception-translation"):
            assert class_level_check(name) is None, f"clean class-level name rejected: {name}"

    def test_single_library_name_rejected(self) -> None:
        for name in ("requests", "pandas", "sqlalchemy", "pydantic"):
            assert class_level_check(name) is not None

    def test_library_as_part_of_class_name_passes(self) -> None:
        # 'pandas-dataframe-debugging' is class-level even though
        # 'pandas' alone would be rejected.
        assert class_level_check("pandas-dataframe-debugging") is None

    def test_all_digits_rejected(self) -> None:
        assert class_level_check("404") is not None
        assert class_level_check("500-503") is not None

    def test_nightwatch_failure_pattern_rejected(self) -> None:
        """The exact failure mode this guardrail exists to block:
        Nightwatch's `test: add unit tests for robothor/engine/alerts.py`
        PR was recreated daily because the skill name was scoped to a
        one-off task. Verify the equivalent pattern would now be
        rejected before the skill is even created."""
        for name in (
            "fix-alerts-test-coverage",
            "debug-runner-iteration-cap",
            "audit-april-22-nightwatch",
        ):
            reason = class_level_check(name)
            assert reason is not None, f"Nightwatch-style name passed: {name}"


class TestHandlerIntegration:
    """The handler should call class_level_check only when Rip 2 is enabled."""

    @pytest.mark.asyncio
    async def test_create_skill_blocked_by_rip2_when_enabled(self) -> None:
        from robothor.engine.tools.handlers.skills import _create_skill

        with patch.dict(os.environ, {"ROBOTHOR_RIP_2_ENABLED": "1"}, clear=True):
            result = await _create_skill(
                {
                    "name": "fix-broken-typecheck",
                    "description": "fix the typecheck failure",
                    "content": "# how to fix this\n\nsteps...",
                },
                ToolContext(agent_id="test"),
            )

        assert result.get("rejected_by") == "class_level_check"
        assert "CLASS-LEVEL umbrella" in result["error"]

    @pytest.mark.asyncio
    async def test_create_skill_allows_one_off_when_rip2_disabled(self) -> None:
        from robothor.engine.tools.handlers.skills import _create_skill

        with patch.dict(os.environ, {}, clear=True):
            result = await _create_skill(
                {
                    "name": "fix-broken-typecheck",
                    "description": "fix the typecheck failure",
                    "content": "# how to fix this\n\nsteps...",
                },
                ToolContext(agent_id="test"),
            )

        # Rip 2 off — the class-level check does not fire. Whatever
        # happens next (write_skill_file path errors, etc.) is fine
        # for this test; we only assert the rejection didn't trigger.
        assert result.get("rejected_by") != "class_level_check"
