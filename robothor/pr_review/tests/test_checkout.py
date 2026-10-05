"""The local clone a review job's worktree is cut from — against a real local git remote."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from robothor.pr_review.checkout import CheckoutError, ensure_checkout, read_at

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str, str]:
    """A bare 'GitHub' with main and a refs/pull/7/head ref one commit ahead."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "agent@example.com")
    _git(work, "config", "user.name", "Agent")
    (work / "CLAUDE.md").write_text("Repo rule: keep functions small.\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "feature")
    head = _git(work, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "update-ref", "refs/pull/7/head", head)
    _git(bare, "branch", "-q", "-D", "feature")
    return bare, head, _git(work, "rev-parse", "main")


async def test_clones_then_fetches_the_pull_request_head(tmp_path, remote):
    bare, head, base = remote
    dest = tmp_path / "clones" / "acme" / "widgets"
    path = await ensure_checkout(
        dest, number=7, head_sha=head, base_branch="main", remote_url=str(bare), token=""
    )
    assert path == dest
    assert _git(dest, "rev-parse", f"{head}^{{commit}}") == head
    assert _git(dest, "rev-parse", "refs/remotes/origin/main") == base
    # Second call reuses the clone and still finds the head.
    again = await ensure_checkout(
        dest, number=7, head_sha=head, base_branch="main", remote_url=str(bare), token=""
    )
    assert again == dest


async def test_unknown_head_is_an_error(tmp_path, remote):
    bare, _head, _ = remote
    with pytest.raises(CheckoutError, match="not found"):
        await ensure_checkout(
            tmp_path / "c",
            number=7,
            head_sha="f" * 40,
            base_branch="main",
            remote_url=str(bare),
            token="",
        )


async def test_rejects_option_like_refs(tmp_path, remote):
    bare, head, _ = remote
    with pytest.raises(CheckoutError):
        await ensure_checkout(
            tmp_path / "c",
            number=7,
            head_sha=head,
            base_branch="--upload-pack=x",
            remote_url=str(bare),
            token="",
        )


async def test_read_at_returns_file_content_at_a_commit(tmp_path, remote):
    bare, head, _ = remote
    dest = tmp_path / "c"
    await ensure_checkout(
        dest, number=7, head_sha=head, base_branch="main", remote_url=str(bare), token=""
    )
    assert "keep functions small" in (await read_at(dest, head, "CLAUDE.md") or "")
    assert await read_at(dest, head, ".github/review-guidelines.md") is None
