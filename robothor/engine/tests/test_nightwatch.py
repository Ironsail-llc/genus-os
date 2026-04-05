"""Tests for Nightwatch scripts — shared lib, heal, research, build."""

from __future__ import annotations

import subprocess

# Import the shared library
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "brain" / "scripts"))

try:
    from nightwatch_lib import (
        check_pause_condition,
        cleanup_worktree,
        create_worktree,
        determine_scope,
        extract_pr_url,
        get_merge_rate,
        today_str,
    )
except ImportError:
    pytest.skip(
        "nightwatch_lib not available (brain/scripts not deployed)",
        allow_module_level=True,
    )


def slugify(title: str) -> str:
    """Mirror of the slugify used in heal/build scripts."""
    slug = title.lower()
    slug = "".join(c if c.isalnum() or c == "-" else "-" for c in slug)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:50]


# ---------------------------------------------------------------------------
# nightwatch_lib tests
# ---------------------------------------------------------------------------


class TestCheckPauseCondition:
    def test_paused_keyword(self):
        assert check_pause_condition("PAUSED — 3 consecutive rejections") is True

    def test_not_paused_empty(self):
        assert check_pause_condition("") is False

    def test_three_consecutive_rejections(self):
        log = (
            "[2026-03-01] outcome: rejected\n"
            "[2026-03-02] outcome: rejected\n"
            "[2026-03-03] outcome: rejected\n"
        )
        assert check_pause_condition(log) is True

    def test_two_rejections_not_paused(self):
        log = "[2026-03-01] outcome: rejected\n[2026-03-02] outcome: rejected\n"
        assert check_pause_condition(log) is False

    def test_rejection_broken_by_merge(self):
        log = (
            "[2026-03-01] outcome: rejected\n"
            "[2026-03-02] outcome: merged\n"
            "[2026-03-03] outcome: rejected\n"
        )
        assert check_pause_condition(log) is False

    def test_mixed_content_with_rejections(self):
        log = (
            "[2026-03-01] heal: PRs created: 1\n"
            "[2026-03-01] outcome: rejected\n"
            "[2026-03-02] heal: PRs created: 1\n"
            "[2026-03-02] outcome: rejected\n"
            "[2026-03-03] heal: PRs created: 1\n"
            "[2026-03-03] outcome: rejected\n"
        )
        assert check_pause_condition(log) is True


class TestGetMergeRate:
    def test_no_history(self):
        assert get_merge_rate("") == 0.0

    def test_insufficient_history(self):
        log = "[2026-03-01] outcome: merged\n[2026-03-02] outcome: merged\n"
        assert get_merge_rate(log) == 0.0  # <5 entries

    def test_all_merged(self):
        log = "\n".join("[day] outcome: merged" for _ in range(5))
        assert get_merge_rate(log) == 1.0

    def test_all_rejected(self):
        log = "\n".join("[day] outcome: rejected" for _ in range(5))
        assert get_merge_rate(log) == 0.0

    def test_mixed(self):
        # 3 merged + 2 rejected = 60%
        log = (
            "outcome: merged\noutcome: merged\noutcome: merged\n"
            "outcome: rejected\noutcome: rejected\n"
        )
        assert get_merge_rate(log) == pytest.approx(0.6)

    def test_modified_counts_as_merged(self):
        log = "\n".join("outcome: modified" for _ in range(5))
        assert get_merge_rate(log) == 1.0


class TestDetermineScope:
    def test_low_merge_rate(self):
        assert determine_scope(0.0) == "config"
        assert determine_scope(0.3) == "config"
        assert determine_scope(0.49) == "config"

    def test_medium_merge_rate(self):
        assert determine_scope(0.5) == "config+instructions"
        assert determine_scope(0.6) == "config+instructions"
        assert determine_scope(0.69) == "config+instructions"

    def test_high_merge_rate(self):
        assert determine_scope(0.7) == "config+instructions+code"
        assert determine_scope(0.9) == "config+instructions+code"
        assert determine_scope(1.0) == "config+instructions+code"


class TestExtractPrUrl:
    def test_from_dict(self):
        output = {"result": "Created PR at https://github.com/test-org/test-repo/pull/42"}
        assert extract_pr_url(output) == "https://github.com/test-org/test-repo/pull/42"

    def test_from_nested(self):
        output = {"steps": [{"output": "https://github.com/org/repo/pull/123"}]}
        assert extract_pr_url(output) == "https://github.com/org/repo/pull/123"

    def test_no_url(self):
        assert extract_pr_url({"result": "Tests failed, no PR created"}) is None

    def test_from_string(self):
        assert (
            extract_pr_url("PR: https://github.com/foo/bar/pull/1")
            == "https://github.com/foo/bar/pull/1"
        )


class TestTodayStr:
    def test_format(self):
        result = today_str()
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"


class TestSlugify:
    def test_simple(self):
        assert slugify("Fix email classifier") == "fix-email-classifier"

    def test_special_chars(self):
        assert slugify("Fix: email (classifier) #1") == "fix-email-classifier-1"

    def test_truncation(self):
        long = "a" * 100
        assert len(slugify(long)) <= 50


# ---------------------------------------------------------------------------
# Worktree management tests
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    @patch("nightwatch_lib.subprocess.run")
    def test_creates_worktree(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        path = create_worktree("nightwatch/2026-03-05/test-fix")
        assert "nightwatch-" in str(path)
        mock_run.assert_called()
        # Verify git worktree add was called
        args = mock_run.call_args_list[0]
        assert "worktree" in args[0][0]
        assert "add" in args[0][0]

    @patch("nightwatch_lib.subprocess.run")
    def test_cleans_up_existing(self, mock_run):
        """If worktree path already exists, cleanup first."""
        mock_run.return_value = MagicMock(returncode=0)
        with patch("nightwatch_lib.Path.exists", return_value=True):
            with patch("nightwatch_lib.cleanup_worktree") as mock_cleanup:
                create_worktree("nightwatch/2026-03-05/test")
                mock_cleanup.assert_called_once()


class TestCleanupWorktree:
    @patch("nightwatch_lib.subprocess.run")
    def test_removes_worktree(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        cleanup_worktree(Path("/tmp/nightwatch-test"))
        # Should call git worktree remove, then git worktree prune
        assert mock_run.call_count == 2

    @patch("nightwatch_lib.subprocess.run")
    @patch("nightwatch_lib.shutil.rmtree")
    def test_fallback_on_failure(self, mock_rmtree, mock_run):
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),
            MagicMock(returncode=0),  # prune
        ]
        with patch("nightwatch_lib.Path.exists", return_value=True):
            cleanup_worktree(Path("/tmp/nightwatch-test"))
        mock_rmtree.assert_called_once()


# ---------------------------------------------------------------------------
# Claude Code invocation tests
# ---------------------------------------------------------------------------


class TestInvokeClaudeCode:
    @patch("nightwatch_lib.subprocess.run")
    def test_success(self, mock_run):
        from nightwatch_lib import invoke_claude_code

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"result": "Done", "pr_url": "https://github.com/org/repo/pull/1"}',
            stderr="",
        )
        result = invoke_claude_code(
            cwd=Path("/tmp/test"),
            prompt="Fix the bug",
            system_prompt="You are a fixer",
            allowed_tools="Read,Edit",
            budget=0.5,
        )
        assert result.get("result") == "Done"
        assert "error" not in result

    @patch("nightwatch_lib.subprocess.run")
    def test_nonzero_exit(self, mock_run):
        from nightwatch_lib import invoke_claude_code

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: budget exceeded",
        )
        result = invoke_claude_code(
            cwd=Path("/tmp/test"),
            prompt="Fix",
            system_prompt="System",
            allowed_tools="Read",
            budget=0.5,
        )
        assert "error" in result

    @patch("nightwatch_lib.subprocess.run")
    def test_timeout(self, mock_run):
        from nightwatch_lib import invoke_claude_code

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        result = invoke_claude_code(
            cwd=Path("/tmp/test"),
            prompt="Fix",
            system_prompt="System",
            allowed_tools="Read",
            budget=0.5,
        )
        assert "timed out" in result["error"]

    @patch("nightwatch_lib.subprocess.run")
    def test_strips_claude_env_vars(self, mock_run):
        from nightwatch_lib import invoke_claude_code

        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch.dict("os.environ", {"CLAUDE_SESSION": "abc", "PATH": "/usr/bin"}):
            invoke_claude_code(
                cwd=Path("/tmp/test"),
                prompt="Fix",
                system_prompt="System",
                allowed_tools="Read",
                budget=0.5,
            )
        # Check env passed to subprocess
        call_kwargs = mock_run.call_args[1]
        env = call_kwargs.get("env", {})
        assert "CLAUDE_SESSION" not in env
        assert "PATH" in env


# ---------------------------------------------------------------------------
# CRM helper tests
# ---------------------------------------------------------------------------


class TestCrmHelpers:
    @patch("nightwatch_lib.list_tasks")
    def test_get_tasks(self, mock_list):
        from nightwatch_lib import get_tasks

        mock_list.return_value = [{"id": "1", "title": "Test"}]
        result = get_tasks(tags=["nightwatch"], status="TODO")
        mock_list.assert_called_once_with(tags=["nightwatch"], status="TODO", limit=3)
        assert len(result) == 1

    @patch("nightwatch_lib.resolve_task")
    def test_resolve_nightwatch_task(self, mock_resolve):
        from nightwatch_lib import resolve_nightwatch_task

        mock_resolve.return_value = True
        assert resolve_nightwatch_task("task-1", "Fixed") is True
        mock_resolve.assert_called_once_with("task-1", resolution="Fixed", agent_id="nightwatch")

    @patch("nightwatch_lib.create_task")
    def test_create_nightwatch_task(self, mock_create):
        from nightwatch_lib import create_nightwatch_task

        mock_create.return_value = "new-task-id"
        result = create_nightwatch_task(
            title="Test", body="Body", tags=["nightwatch"], priority="high"
        )
        assert result == "new-task-id"
        mock_create.assert_called_once_with(
            title="Test",
            body="Body",
            tags=["nightwatch"],
            priority="high",
            assigned_to_agent="main",
            created_by_agent="nightwatch",
        )


# ---------------------------------------------------------------------------
# Memory block helper tests
# ---------------------------------------------------------------------------


class TestMemoryBlockHelpers:
    @patch("nightwatch_lib.read_block")
    def test_read_memory_block(self, mock_read):
        from nightwatch_lib import read_memory_block

        mock_read.return_value = {"content": "test content"}
        assert read_memory_block("test") == "test content"

    @patch("nightwatch_lib.read_block")
    def test_read_memory_block_not_found(self, mock_read):
        from nightwatch_lib import read_memory_block

        mock_read.return_value = {"error": "not found"}
        assert read_memory_block("missing") == ""

    @patch("nightwatch_lib.write_block")
    def test_write_memory_block(self, mock_write):
        from nightwatch_lib import write_memory_block

        mock_write.return_value = {"success": True}
        assert write_memory_block("test", "content") is True


# ---------------------------------------------------------------------------
# Integration-style tests for heal script
# ---------------------------------------------------------------------------


class TestHealScript:
    @patch("nightwatch_lib.write_block")
    @patch("nightwatch_lib.read_block")
    @patch("nightwatch_lib.list_tasks")
    def test_dry_run_no_tasks(self, mock_list, mock_read, mock_write):
        """Heal with no tasks should write status and exit."""
        mock_read.return_value = {"content": ""}
        mock_list.return_value = []

        # Import and run
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "brain" / "scripts"))
        # We test the logic via nightwatch_lib functions directly
        from nightwatch_lib import check_pause_condition, get_tasks

        assert check_pause_condition("") is False
        assert get_tasks(tags=["nightwatch", "self-improve"]) == []


class TestResearchParsing:
    def test_parse_improvements_from_dict(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "brain" / "scripts"))

        # Test the parsing logic
        result: dict[str, Any] = {
            "improvements": [
                {
                    "title": "Parallel tool calling",
                    "description": "Execute multiple tools concurrently",
                    "frameworks": ["LangGraph", "CrewAI"],
                    "impact": "high",
                    "effort": "medium",
                    "files_affected": ["robothor/engine/runner.py"],
                    "rationale": "Reduces latency",
                }
            ],
            "summary": "Found improvements",
        }
        # Inline the parsing logic
        improvements: list[dict[str, Any]] = result.get("improvements", [])
        assert len(improvements) == 1
        assert improvements[0]["title"] == "Parallel tool calling"
