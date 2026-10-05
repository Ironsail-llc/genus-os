"""One git worktree per coding job.

Claude Code never works in the operator's checkout. Each job gets its own
worktree under ``ROBOTHOR_CODING_WORKTREE_ROOT`` (default
``$ROBOTHOR_WORKSPACE/.genus/worktrees``), on a new branch for ``code`` jobs or
a detached ref for ``review``/``readonly`` ones. The branch outlives the
worktree: removing the directory at the end of a job keeps the commits, and a
follow-up re-attaches the same branch at the same path, which is also where
Claude Code filed the session it resumes.

A job never works on a protected branch (``main``/``master`` — the same set the
``git_*`` tools refuse), and a ref that starts with ``-`` is refused before it
reaches git's argv.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from robothor.engine.tools.constants import PROTECTED_BRANCHES

__all__ = [
    "WorktreeError",
    "WorktreeInfo",
    "commits_since",
    "create_worktree",
    "dirty_paths",
    "git_identity",
    "head_sha",
    "is_dirty",
    "remove_worktree",
    "restore_worktree",
    "worktree_path_for",
    "worktree_root",
]

_GIT_TIMEOUT = 60.0


class WorktreeError(RuntimeError):
    """A worktree operation that cannot or must not happen."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str | None
    base_sha: str


def worktree_root() -> Path:
    from robothor.settings import get_settings

    settings = get_settings()
    configured = settings.coding.worktree_root.strip()
    if configured:
        return Path(configured).expanduser()
    workspace = settings.paths.workspace.strip()
    base = Path(workspace).expanduser() if workspace else Path.home() / "robothor"
    return base / ".genus" / "worktrees"


def worktree_path_for(job_id: str) -> Path:
    return worktree_root() / job_id


async def _git(
    cwd: Path | str, *args: str, check: bool = True, strip: bool = True
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise WorktreeError(f"git {args[0]} timed out") from None
    code = proc.returncode or 0
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace").strip()
    if strip:
        stdout = stdout.strip()
    if check and code != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {stderr or stdout}")
    return code, stdout, stderr


def _refuse_option(value: str, what: str) -> None:
    if not value or value.startswith("-") or any(c in value for c in "\0\n "):
        raise WorktreeError(f"invalid {what}: {value!r}")


async def _require_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise WorktreeError(f"{repo} is not a directory")
    code, out, _ = await _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if code != 0 or out != "true":
        raise WorktreeError(f"{repo} is not a git repository")


async def create_worktree(
    repo: Path | str,
    path: Path | str,
    *,
    branch: str | None,
    base_ref: str = "HEAD",
) -> WorktreeInfo:
    """Add a worktree at ``path``: a new ``branch`` from ``base_ref``, or detached."""
    repo, path = Path(repo), Path(path)
    _refuse_option(base_ref, "base ref")
    if branch is not None:
        _refuse_option(branch, "branch name")
        if branch in PROTECTED_BRANCHES:
            raise WorktreeError(f"refusing to work on protected branch {branch!r}")
    await _require_repo(repo)
    if path.exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    _, base_sha, _ = await _git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch is None:
        await _git(repo, "worktree", "add", "--detach", str(path), base_sha)
    else:
        await _git(repo, "worktree", "add", "-b", branch, str(path), base_sha)
    return WorktreeInfo(path=path, branch=branch, base_sha=base_sha)


async def restore_worktree(
    repo: Path | str, path: Path | str, branch: str | None, ref: str = ""
) -> Path:
    """Re-attach a job's branch (or detached ``ref``) at its old path."""
    repo, path = Path(repo), Path(path)
    if path.is_dir():
        return path
    await _git(repo, "worktree", "prune", check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        _refuse_option(branch, "branch name")
        if branch in PROTECTED_BRANCHES:
            raise WorktreeError(f"refusing to work on protected branch {branch!r}")
        await _git(repo, "worktree", "add", str(path), branch)
    else:
        _refuse_option(ref, "ref")
        await _git(repo, "worktree", "add", "--detach", str(path), ref)
    return path


async def remove_worktree(repo: Path | str, path: Path | str) -> None:
    """Remove the worktree directory; its branch and commits stay in ``repo``."""
    repo, path = Path(repo), Path(path)
    if path.exists():
        await _git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    if repo.is_dir():
        await _git(repo, "worktree", "prune", check=False)


async def head_sha(path: Path | str) -> str:
    _, out, _ = await _git(path, "rev-parse", "HEAD")
    return out


async def commits_since(path: Path | str, base_sha: str) -> list[str]:
    """Commits on HEAD that ``base_sha`` does not have, newest first."""
    _refuse_option(base_sha, "base sha")
    _, out, _ = await _git(path, "rev-list", f"{base_sha}..HEAD")
    return [line for line in out.splitlines() if line]


#: Directories a test or build run writes as a side effect. In a repository
#: with no .gitignore they show up as untracked, and they are not work anybody
#: forgot to commit.
_CACHE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", "node_modules"}
)


async def dirty_paths(path: Path | str) -> list[str]:
    """Modified, staged or untracked paths, ignoring tool caches.

    Untracked files count: a new source file Claude Code forgot to ``git add``
    passes a verify run against the working tree while missing from the commit.
    """
    _, out, _ = await _git(path, "status", "--porcelain", "--untracked-files=all", strip=False)
    found: list[str] = []
    for line in out.splitlines():
        entry = line[3:].strip().strip('"')
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if not entry or _CACHE_DIRS & set(Path(entry).parts) or entry.endswith(".pyc"):
            continue
        found.append(entry)
    return found


async def is_dirty(path: Path | str) -> bool:
    return bool(await dirty_paths(path))


async def git_identity(repo: Path | str) -> tuple[str, str] | None:
    """The repository's configured author, for commits made in its worktrees.

    Read with the engine's own environment — the job's environment has a
    private HOME, so the service user's global git config is not visible there.
    """
    _, name, _ = await _git(repo, "config", "--get", "user.name", check=False)
    _, email, _ = await _git(repo, "config", "--get", "user.email", check=False)
    if name and email:
        return name, email
    return None
