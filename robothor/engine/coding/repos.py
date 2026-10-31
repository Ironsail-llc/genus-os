"""Repositories a coding job may work on, put on disk by the engine itself.

``coding_repo_prepare`` names a remote; this module clones it once into
``ROBOTHOR_CODING_WORKSPACE/<owner>/<repo>`` and fetches (then fast-forwards
the default branch) on every later call, so ``claude_code_start`` always has a
current checkout to branch a job from. The workspace is a repo root of its own
(:func:`robothor.engine.coding.jobs.check_repo_path`).

**Only allowlisted remotes.** ``ROBOTHOR_CODING_REMOTES`` names them, as
``owner/repo`` (GitHub) or a full clone URL; anything else is refused before git
runs. The same list decides where :mod:`robothor.engine.coding.publish` may
push.

**The token never touches argv or disk.** A GitHub token reaches git as an
``http.<origin>.extraheader`` set through ``GIT_CONFIG_COUNT``/``KEY``/``VALUE``
environment variables, only for an https remote. The clone URL stored in
``.git/config`` is the plain one, and every error is redacted before it leaves.

**One clone, one writer.** Every git write into a clone holds that clone's
``asyncio.Lock``, so two prepares (or a prepare and a push) never race on its
refs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "PreparedRepo",
    "RemoteSpec",
    "RepoError",
    "allowed_remote",
    "clone_lock",
    "default_branch",
    "ensure_repo",
    "git",
    "git_env",
    "github_token",
    "origin_remote",
    "parse_remote",
    "redact",
    "workspace_dir",
]

GITHUB_HOST = "github.com"
_TIMEOUT_S = 300.0
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHORTHAND = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_SCP = re.compile(r"^[A-Za-z0-9_.-]+@([A-Za-z0-9_.-]+):(.+)$")
_SCHEMES = frozenset({"https", "ssh", "file"})
#: Characters no remote this module accepts ever contains: a remote is never
#: given to a shell, and these would only ever mean an attempt to reach one.
_FORBIDDEN = re.compile(r"[\s;|&$`<>'\"\\]")
_LOCKS: dict[str, asyncio.Lock] = {}


class RepoError(ValueError):
    """Why a repository could not be prepared, said so the agent can act on it."""


@dataclass(frozen=True)
class RemoteSpec:
    """One remote: where it is cloned from and the owner/repo it is filed under."""

    owner: str
    name: str
    url: str
    host: str

    @property
    def key(self) -> tuple[str, ...]:
        """What two spellings of the same remote share."""
        if self.host == GITHUB_HOST:
            return (GITHUB_HOST, self.owner.lower(), self.name.lower())
        return ("url", self.url.rstrip("/").removesuffix(".git"))

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PreparedRepo:
    path: Path
    remote: RemoteSpec
    default_branch: str
    head_sha: str
    cloned: bool


def _settings() -> object:
    from robothor.settings import get_settings

    return get_settings().coding


def _check_segment(value: str, raw: str) -> str:
    if not _SEGMENT.match(value) or value in (".", "..") or value.startswith("-"):
        raise RepoError(f"remote {raw!r} does not end in an owner/repo pair")
    return value


def parse_remote(raw: str) -> RemoteSpec:
    """``owner/repo`` or a clone URL, as a :class:`RemoteSpec`; ``RepoError`` if neither."""
    raw = (raw or "").strip()
    if not raw:
        raise RepoError("repo is required: owner/repo or a clone URL")
    if raw.startswith("-") or "::" in raw or _FORBIDDEN.search(raw):
        raise RepoError(f"remote {raw!r} is not a plain owner/repo or clone URL")
    short = _SHORTHAND.match(raw)
    if short and "://" not in raw:
        owner = _check_segment(short.group(1), raw)
        name = _check_segment(short.group(2).removesuffix(".git"), raw)
        return RemoteSpec(owner, name, f"https://{GITHUB_HOST}/{owner}/{name}.git", GITHUB_HOST)
    scp = _SCP.match(raw)
    if scp and "://" not in raw:
        host, path = scp.group(1).lower(), scp.group(2)
    else:
        parsed = urlparse(raw)
        if parsed.scheme not in _SCHEMES:
            raise RepoError(f"remote {raw!r} must be owner/repo or an https, ssh or file URL")
        host, path = (parsed.hostname or "").lower(), parsed.path
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise RepoError(f"remote {raw!r} does not end in an owner/repo pair")
    owner = _check_segment(parts[-2], raw)
    name = _check_segment(parts[-1].removesuffix(".git"), raw)
    return RemoteSpec(owner, name, raw, host)


def allowed_remote(raw: str) -> RemoteSpec:
    """The allowlist's entry for ``raw``, or ``RepoError`` naming the setting."""
    spec = parse_remote(raw)
    configured = str(getattr(_settings(), "remotes", "") or "")
    entries = [e.strip() for e in configured.split(",") if e.strip()]
    if not entries:
        raise RepoError(
            "no remote may be cloned until ROBOTHOR_CODING_REMOTES names the allowed "
            "repositories (comma-separated owner/repo or clone URLs)"
        )
    for entry in entries:
        with contextlib.suppress(RepoError):
            allowed = parse_remote(entry)
            if allowed.key == spec.key:
                return allowed
    raise RepoError(f"remote {spec.slug} is not in ROBOTHOR_CODING_REMOTES")


def workspace_dir() -> Path:
    """``ROBOTHOR_CODING_WORKSPACE``, or ``RepoError`` naming it when unset."""
    configured = str(getattr(_settings(), "workspace", "") or "").strip()
    if not configured:
        raise RepoError(
            "coding_repo_prepare is off until ROBOTHOR_CODING_WORKSPACE names the directory "
            "repositories are cloned into"
        )
    return Path(configured).expanduser()


def github_token(tenant_id: str) -> str:
    """``GITHUB_TOKEN`` from the vault (blocking: call it in a thread)."""
    from robothor.secrets import resolve_secret

    return resolve_secret("GITHUB_TOKEN", tenant_id=tenant_id).value or ""


def _basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def git_env(url: str, token: str) -> dict[str, str]:
    """The environment git runs with: no prompt, and the token as a header for https."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    parsed = urlparse(url)
    if token and parsed.scheme == "https" and parsed.hostname:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.https://{parsed.hostname}/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {_basic(token)}"
    return env


def redact(text: str, token: str) -> str:
    """``text`` with the token, and its basic-auth form, removed."""
    if token:
        text = text.replace(_basic(token), "***").replace(token, "***")
    return text


def clone_lock(path: Path | str) -> asyncio.Lock:
    """The lock every git write into the clone at ``path`` holds."""
    key = str(Path(path).expanduser().resolve())
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


async def git(
    cwd: Path, *args: str, url: str = "", token: str = "", check: bool = True
) -> tuple[int, str]:
    """Run git (never a shell) with :func:`git_env`; ``RepoError`` on failure when ``check``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=git_env(url, token),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        raise RepoError(f"git {args[0]} timed out") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    code = proc.returncode or 0
    if check and code != 0:
        detail = redact(err.decode(errors="replace").strip(), token)[:400]
        raise RepoError(f"git {args[0]} failed: {detail}")
    return code, out.decode(errors="replace").strip()


async def default_branch(dest: Path) -> str:
    """The branch origin/HEAD names, else the one checked out."""
    code, ref = await git(dest, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if code == 0 and ref.startswith("origin/"):
        return ref.removeprefix("origin/")
    _, current = await git(dest, "rev-parse", "--abbrev-ref", "HEAD")
    return current


async def origin_remote(repo: Path) -> RemoteSpec:
    """The allowlisted remote ``repo``'s origin points at, or ``RepoError``."""
    code, url = await git(repo, "remote", "get-url", "origin", check=False)
    if code != 0 or not url:
        raise RepoError(
            f"{repo} has no origin remote; only clones of a ROBOTHOR_CODING_REMOTES entry "
            "(coding_repo_prepare) can be published"
        )
    return allowed_remote(url)


async def ensure_repo(raw: str, *, token: str) -> PreparedRepo:
    """Clone ``raw`` once into the coding workspace, or fetch and fast-forward it."""
    root = workspace_dir()
    spec = allowed_remote(raw)
    dest = root / spec.owner / spec.name
    async with clone_lock(dest):
        cloned = not (dest / ".git").exists()
        if cloned:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                await git(
                    dest.parent, "clone", "--quiet", "--", spec.url, str(dest),
                    url=spec.url, token=token,
                )  # fmt: skip
            except RepoError:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        else:
            current = await origin_remote(dest)
            if current.key != spec.key:
                raise RepoError(f"{dest} is a clone of {current.slug}, not {spec.slug}")
            await git(dest, "fetch", "--quiet", "--prune", "origin", url=spec.url, token=token)
        branch = await default_branch(dest)
        if not cloned:
            code, _ = await git(
                dest, "merge", "--ff-only", "--quiet", f"origin/{branch}", check=False
            )
            if code != 0:
                raise RepoError(
                    f"the clone's {branch} has diverged from origin/{branch}; it was fetched "
                    "but not fast-forwarded"
                )
        _, head = await git(dest, "rev-parse", "HEAD")
    return PreparedRepo(
        path=dest.resolve(), remote=spec, default_branch=branch, head_sha=head, cloned=cloned
    )
