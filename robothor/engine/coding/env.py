"""The environment a Claude Code job runs in: an allowlist, built from nothing.

The same rule as :mod:`robothor.engine.exec_env`, applied harder. An agent's
``exec`` child starts from the engine's environment and has things taken away;
a Claude Code child starts EMPTY and has things added, because it is a whole
autonomous agent with a shell of its own, and the engine's environment on a
systemd instance is the ~50 credentials ``load-secrets.sh`` decrypted at boot.

What it gets:

* the process essentials — ``PATH``, ``LANG``, ``LC_*``, ``TERM``, ``TZ``,
  ``TMPDIR`` — each still put through the credential-value gate, because a DSN
  has been found sitting in ``LC_PAPER`` before;
* ``HOME`` and ``CLAUDE_CONFIG_DIR`` pointing at a private per-job directory,
  ``$XDG_CONFIG_HOME/robothor/claude-code/<job>`` (``~/.config`` when unset).
  That directory is writable under the engine unit (``ProtectHome=read-only``
  with ``~/.config/robothor`` in ``ReadWritePaths``), and it means Claude Code
  reads none of the service user's own ``~/.claude`` — no personal settings,
  plugins, hooks, memory or MCP servers — and ``gh``/``git`` find no personal
  login either;
* ``CLAUDE_CODE_OAUTH_TOKEN``, resolved VAULT FIRST through
  :mod:`robothor.secrets` — the vault is the store for application
  credentials, the environment only a bootstrap fallback;
* ``GH_TOKEN`` only when the job was explicitly granted it;
* a git identity, so a commit in the worktree does not fail for want of one.

``ROBOTHOR_CLAUDE_CODE_AUTH=host`` is the one escape hatch, for a developer box
whose service user is logged in to Claude Code itself: it keeps the real
``HOME`` (and so the real login) and passes no token. Secrets are still not
inherited. It is not for production — the service user's personal Claude Code
configuration (plugins, memory) then applies to every job.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from robothor.constants import DEFAULT_TENANT
from robothor.engine.exec_env import looks_like_a_credential_name, looks_like_a_credential_value
from robothor.secrets import resolve_secret

__all__ = [
    "AUTH_MODE_ENV",
    "TOKEN_ENV",
    "auth_mode",
    "build_claude_env",
    "build_verify_env",
    "job_config_dir",
    "resolve_oauth_token",
]

#: The credential Claude Code reads for subscription auth (`claude setup-token`).
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: ``token`` (default) or ``host``. See the module docstring.
AUTH_MODE_ENV = "ROBOTHOR_CLAUDE_CODE_AUTH"

#: Names a child needs to be a working process. Values still go through the
#: credential-value gate.
_ESSENTIALS = ("PATH", "LANG", "TERM", "TZ", "TMPDIR")
_LOCALE_PREFIX = "LC_"

#: Quietens Claude Code's own background traffic: no self-update of a binary
#: the operator installed, no telemetry from a service.
_FIXED = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_GENERIC_GIT_NAME = "Genus Agent"
_GENERIC_GIT_EMAIL = "agent@example.com"


def job_config_dir(job_id: str, *, base: dict[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/robothor/claude-code/<job_id>``, ~/.config when unset."""
    if not _JOB_ID_RE.match(job_id or ""):
        raise ValueError(f"not a job id: {job_id!r}")
    source = os.environ if base is None else base
    xdg = (source.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        root = Path(xdg).expanduser()
    else:
        home = (source.get("HOME") or "").strip()
        root = (Path(home) if home else Path.home()) / ".config"
    return root / "robothor" / "claude-code" / job_id


def resolve_oauth_token(tenant_id: str = DEFAULT_TENANT) -> str | None:
    """The Claude Code token for this tenant, vault first, or None."""
    return resolve_secret(TOKEN_ENV, tenant_id=tenant_id).value


def auth_mode(source: dict[str, str] | None = None) -> str:
    """``token`` or ``host``: from ``source`` when it names one, else the setting."""
    raw = (source or {}).get(AUTH_MODE_ENV)
    if raw is None:
        from robothor.settings import get_settings

        raw = get_settings().coding.claude_code_auth
    return "host" if str(raw).strip().lower() == "host" else "token"


def _essentials(source: dict[str, str]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name, value in source.items():
        if name not in _ESSENTIALS and not name.startswith(_LOCALE_PREFIX):
            continue
        if looks_like_a_credential_name(name) or looks_like_a_credential_value(value):
            continue
        kept[name] = value
    kept.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return kept


def _private_home(job_id: str, source: dict[str, str]) -> Path:
    home = job_config_dir(job_id, base=source)
    home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        home.chmod(0o700)
    (home / ".claude").mkdir(exist_ok=True)
    return home


def _base_env(
    job_id: str,
    source: dict[str, str],
    git_identity: tuple[str, str] | None,
) -> dict[str, str]:
    env = _essentials(source)
    env.update(_FIXED)
    if auth_mode(source) == "host":
        real_home = (source.get("HOME") or "").strip() or str(Path.home())
        env["HOME"] = real_home
    else:
        home = _private_home(job_id, source)
        env["HOME"] = str(home)
        env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        # An empty XDG root under the private home: `gh`, `git` and friends
        # find no personal login there.
        env["XDG_CONFIG_HOME"] = str(home / ".config")
    name, email = git_identity or (_GENERIC_GIT_NAME, _GENERIC_GIT_EMAIL)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = email
    # Never stop on an editor or a pager nobody can see.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    env["GIT_PAGER"] = "cat"
    return env


def build_claude_env(
    *,
    job_id: str,
    oauth_token: str | None,
    base: dict[str, str] | None = None,
    github_token: str | None = None,
    git_identity: tuple[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment for one job's ``claude -p`` subprocess."""
    source = dict(os.environ) if base is None else dict(base)
    env = _base_env(job_id, source, git_identity)
    if auth_mode(source) != "host" and oauth_token:
        env[TOKEN_ENV] = oauth_token
    if github_token:
        env["GH_TOKEN"] = github_token
    return env


def build_verify_env(
    *,
    job_id: str,
    base: dict[str, str] | None = None,
    git_identity: tuple[str, str] | None = None,
) -> dict[str, str]:
    """The environment the acceptance command runs in: no credential at all."""
    source = dict(os.environ) if base is None else dict(base)
    return _base_env(job_id, source, git_identity)
