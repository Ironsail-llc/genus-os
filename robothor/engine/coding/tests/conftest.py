"""Shared fixtures for the Claude Code driver tests.

Every test here works in ``tmp_path``: a throwaway git repository, a throwaway
worktree root and a throwaway config home. Nothing touches the operator's
workspace, HOME or database.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A repository on a feature-safe default branch with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Alice Example")
    git(repo, "config", "user.email", "agent@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def coding_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the worktree root and the per-job config home into tmp_path."""
    root = tmp_path / "worktrees"
    monkeypatch.setenv("ROBOTHOR_CODING_WORKTREE_ROOT", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("ROBOTHOR_CLAUDE_CODE_AUTH", raising=False)
    return root
