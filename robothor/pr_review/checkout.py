"""One local clone per reviewed repository, fetched to the pull request's head.

A review job's worktree is cut from this clone (pr_review_prepare starts the
job with ``base_ref=<head sha>``), so the clone must hold the head commit and the base
branch for ``git merge-base``. The GitHub token reaches git only through
``GIT_CONFIG_*`` environment variables as an ``http.extraHeader``: it is
never written to ``.git/config`` and never appears in an argv.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
from pathlib import Path

__all__ = ["CheckoutError", "ensure_checkout", "read_at"]

_TIMEOUT_S = 300.0
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class CheckoutError(RuntimeError):
    pass


def _git_env(token: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


async def _git(cwd: Path, *args: str, token: str = "", check: bool = True) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=_git_env(token),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CheckoutError(f"git {args[0]} timed out") from None
    finally:
        # Cancelled (the tool's own deadline, or the run ending): never leave a
        # clone or fetch running unsupervised on the host.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    code = proc.returncode or 0
    if check and code != 0:
        raise CheckoutError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:400]}")
    return code, out.decode(errors="replace").strip()


async def ensure_checkout(
    dest: Path,
    *,
    number: int,
    head_sha: str,
    base_branch: str,
    remote_url: str,
    token: str,
) -> Path:
    """Clone (once) and fetch the PR head and base branch; return the clone path."""
    if not _SHA_RE.match(head_sha or ""):
        raise CheckoutError(f"head sha {head_sha!r} is not a commit sha")
    if base_branch.startswith("-") or not _REF_RE.match(base_branch or ""):
        raise CheckoutError(f"base branch {base_branch!r} is not a plain branch name")
    if remote_url.startswith("-"):
        raise CheckoutError("remote url may not start with '-'")
    dest = Path(dest)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _git(
            dest.parent,
            "clone",
            "--quiet",
            "--no-checkout",
            "--",
            remote_url,
            str(dest),
            token=token,
        )
    await _git(
        dest,
        "fetch",
        "--quiet",
        "--no-tags",
        "origin",
        f"+refs/pull/{int(number)}/head:refs/genus/pr/{int(number)}",
        f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
        token=token,
    )
    code, _ = await _git(dest, "cat-file", "-e", f"{head_sha}^{{commit}}", check=False)
    if code != 0:
        raise CheckoutError(f"head {head_sha[:12]} not found after fetching pull/{number}/head")
    return dest


async def merge_conflicts(repo: Path, base: str, head: str) -> list[str] | None:
    """The files that conflict when ``head`` is merged into ``base``; None when unknown.

    ``git merge-tree --write-tree`` merges in memory and writes only objects
    (never a ref, the index or a work tree) into the reviewer's own clone, on
    the host, before the read-only review job starts.
    """
    if base.startswith("-") or head.startswith("-"):
        return None
    try:
        code, out = await _git(
            Path(repo),
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            base,
            head,
            check=False,
        )
    except (CheckoutError, OSError):
        return None
    lines = out.splitlines()
    # The first line is the merged tree's id; without it git refused the merge.
    if code not in (0, 1) or not lines or not _SHA_RE.match(lines[0].strip()):
        return None
    if code == 0:
        return []
    return list(dict.fromkeys(line for line in lines[1:] if line.strip()))


async def read_at(repo: Path, sha: str, relpath: str, max_chars: int = 20_000) -> str | None:
    """A file's content at ``sha`` (a commit or a ref such as ``origin/main``), or None."""
    if relpath.startswith("-") or ".." in relpath.split("/") or sha.startswith("-"):
        return None
    code, out = await _git(Path(repo), "show", f"{sha}:{relpath}", check=False)
    if code != 0:
        return None
    return out[:max_chars]
