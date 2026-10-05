"""One git worktree per coding job, never on a protected branch."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.coding.tests.conftest import git
from robothor.engine.coding.worktree import (
    WorktreeError,
    commits_since,
    create_worktree,
    is_dirty,
    remove_worktree,
    restore_worktree,
    worktree_path_for,
)


def test_default_root_is_under_the_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("ROBOTHOR_CODING_WORKTREE_ROOT", raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "ws"))
    assert worktree_path_for("abc") == tmp_path / "ws" / ".genus" / "worktrees" / "abc"


def test_configured_root_wins(coding_env):
    assert worktree_path_for("abc") == coding_env / "abc"


async def test_new_branch_worktree_and_commit_detection(git_repo, coding_env):
    path = worktree_path_for("job1")
    info = await create_worktree(git_repo, path, branch="genus/cc-job1", base_ref="HEAD")

    assert info.path == path and path.is_dir()
    assert info.branch == "genus/cc-job1"
    assert info.base_sha == git(git_repo, "rev-parse", "HEAD")
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "genus/cc-job1"
    assert await commits_since(path, info.base_sha) == []
    assert await is_dirty(path) is False

    (path / "x.txt").write_text("x")
    assert await is_dirty(path) is True
    git(path, "add", "x.txt")
    git(path, "commit", "-q", "-m", "add x")
    commits = await commits_since(path, info.base_sha)
    assert commits == [git(path, "rev-parse", "HEAD")]

    await remove_worktree(git_repo, path)
    assert not path.exists()
    # The branch -- and so the work -- survives the worktree.
    assert git(git_repo, "rev-parse", "genus/cc-job1") == commits[0]

    restored = await restore_worktree(git_repo, path, "genus/cc-job1")
    assert restored.is_dir()
    assert (restored / "x.txt").read_text() == "x"


async def test_tool_caches_do_not_count_as_uncommitted_work(git_repo, coding_env):
    """Running the tests writes __pycache__; that is not work left uncommitted.

    Found by the real end-to-end run: a repo with no .gitignore, a verify
    command that runs pytest, and a job that failed "uncommitted changes" on
    the bytecode cache pytest itself had just written.
    """
    from robothor.engine.coding.worktree import dirty_paths

    path = worktree_path_for("jobc")
    await create_worktree(git_repo, path, branch="genus/cc-jobc")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"x")
    (path / ".pytest_cache").mkdir()
    (path / ".pytest_cache" / "README.md").write_text("x")
    assert await is_dirty(path) is False

    (path / "new_module.py").write_text("x = 1\n")  # an untracked SOURCE file is work
    assert await is_dirty(path) is True
    assert await dirty_paths(path) == ["new_module.py"]


async def test_detached_worktree_for_review(git_repo, coding_env):
    path = worktree_path_for("job2")
    info = await create_worktree(git_repo, path, branch=None, base_ref="main")
    assert info.branch is None
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


@pytest.mark.parametrize("branch", ["main", "master"])
async def test_protected_branches_are_refused(git_repo, coding_env, branch):
    with pytest.raises(WorktreeError, match="protected"):
        await create_worktree(git_repo, worktree_path_for("job3"), branch=branch)


async def test_option_shaped_refs_are_refused(git_repo, coding_env):
    with pytest.raises(WorktreeError):
        await create_worktree(git_repo, worktree_path_for("job4"), branch="-b", base_ref="HEAD")
    with pytest.raises(WorktreeError):
        await create_worktree(
            git_repo, worktree_path_for("job5"), branch="ok", base_ref="--upload-pack=x"
        )


async def test_not_a_repository_is_a_clear_error(tmp_path, coding_env):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        await create_worktree(plain, worktree_path_for("job6"), branch="b")


async def test_removing_an_already_gone_worktree_is_quiet(git_repo, coding_env):
    await remove_worktree(git_repo, Path(coding_env) / "never-existed")
