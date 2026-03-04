"""Tests for git tools and no_main_branch_push guardrail."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.guardrails import GuardrailEngine

# ─── Guardrail: no_main_branch_push ─────────────────────────────────


class TestNoMainBranchPushGuardrail:
    def test_blocks_git_branch_main(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("git_branch", {"branch_name": "main"})
        assert not result.allowed
        assert result.action == "blocked"
        assert "no_main_branch_push" in result.guardrail_name

    def test_blocks_git_branch_master(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("git_branch", {"branch_name": "master"})
        assert not result.allowed

    def test_allows_git_branch_feature(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution(
            "git_branch", {"branch_name": "nightwatch/2026-03-04/fix-classifier"}
        )
        assert result.allowed

    def test_blocks_exec_git_push_main(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("exec", {"command": "git push origin main"})
        assert not result.allowed
        assert "no_main_branch_push" in result.guardrail_name

    def test_blocks_exec_git_push_master(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("exec", {"command": "git push origin master"})
        assert not result.allowed

    def test_allows_exec_git_push_feature(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution(
            "exec", {"command": "git push origin nightwatch/fix-thing"}
        )
        assert result.allowed

    def test_allows_non_git_tools(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("read_file", {"path": "/tmp/x"})
        assert result.allowed

    def test_allows_git_status(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("git_status", {})
        assert result.allowed

    def test_allows_git_diff(self):
        engine = GuardrailEngine(enabled_policies=["no_main_branch_push"])
        result = engine.check_pre_execution("git_diff", {"staged": True})
        assert result.allowed


# ─── Git tool schemas ────────────────────────────────────────────────


class TestGitToolSchemas:
    """Verify git tools are registered in the tool registry."""

    def test_git_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for tool_name in [
                "git_status",
                "git_diff",
                "git_branch",
                "git_commit",
                "git_push",
                "create_pull_request",
            ]:
                assert tool_name in registry._schemas, f"{tool_name} not in registry"

    def test_git_status_in_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "git_status" in READONLY_TOOLS
        assert "git_diff" in READONLY_TOOLS

    def test_git_write_tools_not_readonly(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "git_commit" not in READONLY_TOOLS
        assert "git_push" not in READONLY_TOOLS
        assert "git_branch" not in READONLY_TOOLS
        assert "create_pull_request" not in READONLY_TOOLS

    def test_git_tools_in_set(self):
        from robothor.engine.tools import GIT_TOOLS

        assert len(GIT_TOOLS) == 6
        assert "git_status" in GIT_TOOLS
        assert "create_pull_request" in GIT_TOOLS


# ─── Git tool executors (mocked subprocess) ─────────────────────────


class TestGitStatusExecutor:
    @pytest.mark.asyncio
    async def test_git_status_success(self):
        mock_result = MagicMock()
        mock_result.stdout = "## main\n M file.py\n?? new.txt"
        mock_result.returncode = 0

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool("git_status", {}, workspace="/tmp/repo")
            assert result["exit_code"] == 0
            assert "file.py" in result["status"]
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_git_status_custom_path(self):
        mock_result = MagicMock()
        mock_result.stdout = "## main"
        mock_result.returncode = 0

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_sync_tool

            _handle_sync_tool("git_status", {"path": "/custom/repo"}, workspace="/tmp/repo")
            call_args = mock_run.call_args
            assert call_args.kwargs.get("cwd") == "/custom/repo"


class TestGitDiffExecutor:
    def test_git_diff_unstaged(self):
        mock_result = MagicMock()
        mock_result.stdout = "diff --git a/file.py b/file.py\n+new line"
        mock_result.returncode = 0

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool("git_diff", {}, workspace="/tmp/repo")
            assert result["exit_code"] == 0
            assert "+new line" in result["diff"]
            cmd = mock_run.call_args[0][0]
            assert "--cached" not in cmd

    def test_git_diff_staged(self):
        mock_result = MagicMock()
        mock_result.stdout = "staged changes"
        mock_result.returncode = 0

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_sync_tool

            _handle_sync_tool("git_diff", {"staged": True}, workspace="/tmp/repo")
            cmd = mock_run.call_args[0][0]
            assert "--cached" in cmd


class TestGitBranchExecutor:
    def test_create_branch_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool(
                "git_branch",
                {"branch_name": "nightwatch/2026-03-04/fix"},
                workspace="/tmp/repo",
            )
            assert result["created"] is True
            assert result["branch"] == "nightwatch/2026-03-04/fix"

    def test_rejects_main_branch(self):
        from robothor.engine.tools import _handle_sync_tool

        result = _handle_sync_tool("git_branch", {"branch_name": "main"}, workspace="/tmp/repo")
        assert "error" in result
        assert "protected" in result["error"]

    def test_rejects_master_branch(self):
        from robothor.engine.tools import _handle_sync_tool

        result = _handle_sync_tool("git_branch", {"branch_name": "master"}, workspace="/tmp/repo")
        assert "error" in result

    def test_missing_branch_name(self):
        from robothor.engine.tools import _handle_sync_tool

        result = _handle_sync_tool("git_branch", {}, workspace="/tmp/repo")
        assert "error" in result


class TestGitCommitExecutor:
    def test_commit_success_with_files(self):
        # Mock branch check, git add, git commit, and rev-parse
        branch_result = MagicMock(stdout="nightwatch/fix\n", returncode=0)
        add_result = MagicMock(returncode=0, stderr="")
        commit_result = MagicMock(
            returncode=0, stdout="[nightwatch/fix abc1234] fix thing\n", stderr=""
        )
        hash_result = MagicMock(stdout="abc1234567890\n", returncode=0)

        call_count = 0
        results = [branch_result, add_result, commit_result, hash_result]

        def side_effect(*args, **kwargs):
            nonlocal call_count
            r = results[call_count]
            call_count += 1
            return r

        with patch("robothor.engine.tools.subprocess.run", side_effect=side_effect):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool(
                "git_commit",
                {"message": "fix: thing", "files": ["file.py"]},
                workspace="/tmp/repo",
            )
            assert result["committed"] is True
            assert result["sha"] == "abc123456789"

    def test_commit_rejects_main_branch(self):
        branch_result = MagicMock(stdout="main\n", returncode=0)

        with patch("robothor.engine.tools.subprocess.run", return_value=branch_result):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool(
                "git_commit",
                {"message": "fix: thing"},
                workspace="/tmp/repo",
            )
            assert "error" in result
            assert "protected" in result["error"]

    def test_commit_stages_all_when_no_files(self):
        branch_result = MagicMock(stdout="feature\n", returncode=0)
        add_result = MagicMock(returncode=0, stderr="")
        commit_result = MagicMock(returncode=0, stdout="committed", stderr="")
        hash_result = MagicMock(stdout="abc1234567890\n", returncode=0)

        calls = []

        def side_effect(cmd, **kwargs):
            calls.append(cmd)
            results = [branch_result, add_result, commit_result, hash_result]
            return results[len(calls) - 1]

        with patch("robothor.engine.tools.subprocess.run", side_effect=side_effect):
            from robothor.engine.tools import _handle_sync_tool

            _handle_sync_tool(
                "git_commit",
                {"message": "fix: thing"},
                workspace="/tmp/repo",
            )
            # Second call should be git add -A
            assert calls[1] == ["git", "add", "-A"]

    def test_commit_requires_message(self):
        from robothor.engine.tools import _handle_sync_tool

        result = _handle_sync_tool("git_commit", {}, workspace="/tmp/repo")
        assert "error" in result
        assert "message" in result["error"]


class TestGitPushExecutor:
    def test_push_success(self):
        branch_result = MagicMock(stdout="nightwatch/fix\n", returncode=0)
        push_result = MagicMock(returncode=0, stdout="pushed\n", stderr="")

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            r = [branch_result, push_result][call_count]
            call_count += 1
            return r

        with patch("robothor.engine.tools.subprocess.run", side_effect=side_effect):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool("git_push", {}, workspace="/tmp/repo")
            assert result["pushed"] is True
            assert result["branch"] == "nightwatch/fix"

    def test_push_rejects_main(self):
        branch_result = MagicMock(stdout="main\n", returncode=0)

        with patch("robothor.engine.tools.subprocess.run", return_value=branch_result):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool("git_push", {}, workspace="/tmp/repo")
            assert "error" in result
            assert "protected" in result["error"]

    def test_push_rejects_master(self):
        branch_result = MagicMock(stdout="master\n", returncode=0)

        with patch("robothor.engine.tools.subprocess.run", return_value=branch_result):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool("git_push", {}, workspace="/tmp/repo")
            assert "error" in result


class TestCreatePullRequestExecutor:
    def test_create_pr_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo/pull/42\n"
        mock_result.stderr = ""

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool(
                "create_pull_request",
                {"title": "fix: thing", "body": "Fixed the thing"},
                workspace="/tmp/repo",
            )
            assert result["created"] is True
            assert result["draft"] is True
            assert "42" in result["url"]

            # Verify --draft flag
            cmd = mock_run.call_args[0][0]
            assert "--draft" in cmd

    def test_pr_always_includes_nightwatch_label(self):
        mock_result = MagicMock(returncode=0, stdout="https://github.com/pull/1\n", stderr="")

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result) as mock_run:
            from robothor.engine.tools import _handle_sync_tool

            _handle_sync_tool(
                "create_pull_request",
                {"title": "fix", "body": "body", "labels": ["bug"]},
                workspace="/tmp/repo",
            )
            cmd = mock_run.call_args[0][0]
            label_idx = cmd.index("--label")
            labels = cmd[label_idx + 1]
            assert "nightwatch" in labels
            assert "bug" in labels

    def test_pr_requires_title(self):
        from robothor.engine.tools import _handle_sync_tool

        result = _handle_sync_tool(
            "create_pull_request",
            {"body": "body"},
            workspace="/tmp/repo",
        )
        assert "error" in result

    def test_pr_failure(self):
        mock_result = MagicMock(returncode=1, stdout="", stderr="no remote configured")

        with patch("robothor.engine.tools.subprocess.run", return_value=mock_result):
            from robothor.engine.tools import _handle_sync_tool

            result = _handle_sync_tool(
                "create_pull_request",
                {"title": "fix", "body": "body"},
                workspace="/tmp/repo",
            )
            assert "error" in result
