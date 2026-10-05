"""The fence a coding job runs inside: Claude Code's Bash sandbox plus file rules.

A ``code`` job is a whole autonomous agent with a shell, running as the
engine's service user — whose home holds SSH keys, cloud credentials, the
Claude Code login and the instance's own secrets. Two mechanisms, because
Claude Code has two kinds of tool:

* **Bash** runs under Claude Code's own sandbox (bubblewrap + a filtering
  proxy), configured per job through ``--settings``:
  ``failIfUnavailable`` so a host without the sandbox fails the job instead of
  running it bare, ``allowUnsandboxedCommands: false`` so the model cannot ask
  its way out, ``denyRead`` over every credential and instance-data path,
  ``allowWrite`` over exactly the job's worktree and the slices of the
  repository's git directory a ``git commit`` in a linked worktree writes
  (objects, the job branch's ref and reflog directory, the worktree's own admin
  directory) — never ``hooks/`` or ``config``, which are code execution — and
  no network unless ``ROBOTHOR_CODING_ALLOWED_DOMAINS`` names some.
  ``review``/``readonly`` jobs write nothing (the sandbox's default write
  access to the working directory is denied), never reach the network, and
  keep ``autoAllowBashIfSandboxed`` off so their git allowlist stays the gate.
* **Read/Edit/Write/Grep/Glob** are not sandboxed — they run in the CLI process
  itself — so they are fenced by permission rules: ``Edit``/``Write`` only
  under the worktree (``--permission-mode dontAsk`` refuses everything not
  allowed), and ``Read``/``Edit``/``Write`` explicitly denied on the same
  secret paths. Absolute paths in a rule take the ``//abs/path`` form.

The sandbox governs only commands Claude Code runs; Claude Code itself (the
unsandboxed parent) still reads its own login under ``~/.claude``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robothor.engine.coding.runner import MODES

__all__ = [
    "JobPaths",
    "allowed_domains",
    "build_permissions",
    "build_sandbox_settings",
    "live_workspace",
    "sandbox_problems",
    "secret_paths",
    "unwritable_login_paths",
]

#: Under the service user's home: credentials of every kind, the Claude Code
#: login, and the instance's own state.
_HOME_SECRETS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".netrc",
    ".git-credentials",
    ".pgpass",
    ".docker",
    ".kube",
    ".config",
    ".claude",
    ".claude.json",
    ".robothor",
    ".local/share/keyrings",
)

#: Under the live workspace: agent memory and instance config.
_WORKSPACE_SECRETS: tuple[str, ...] = ("brain", ".robothor", "local")

#: System-wide: decrypted secrets (tmpfs) and the instance env file.
_SYSTEM_SECRETS: tuple[str, ...] = ("/run/robothor", "/etc/robothor")

#: Read-only git commands, in Claude Code's permission-rule syntax. No
#: ``git grep`` (``-O``/``--open-files-in-pager`` runs a program; the Grep tool
#: does the same job) and no ``gh`` (read-only modes have no network).
READ_ONLY_BASH: tuple[str, ...] = (
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git status:*)",
    "Bash(git blame:*)",
    "Bash(git rev-parse:*)",
    "Bash(git merge-base:*)",
    "Bash(git ls-files:*)",
    "Bash(git branch --list:*)",
)

#: Forms of the allowed read-only commands that write a file (``--output``),
#: run a configured program (``--ext-diff``, ``--textconv``; ``--ext`` is the
#: abbreviation git accepts) or read outside the repository (``--no-index``).
#: The sandbox stops the writes regardless; these stop the attempt.
_READ_ONLY_BASH_DENY: tuple[str, ...] = tuple(
    f"Bash(git *{flag}*)"
    for flag in ("--output", "--ext-diff", "--ext", "--textconv", "--no-index")
)

#: Never, in any mode: these leave the worktree. A job's work reaches a remote
#: only through the orchestrator's own git/GitHub tools, after verification.
NEVER: tuple[str, ...] = (
    "Bash(git push:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr merge:*)",
    "Bash(gh pr review:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh api:*)",
    "WebFetch",
    "WebSearch",
)


@dataclass(frozen=True)
class JobPaths:
    """Where one job may and may not touch."""

    home: Path
    workspace: Path | None
    worktree: Path
    git_common_dir: Path | None = None
    git_admin_dir: Path | None = None
    branch: str | None = None


def live_workspace() -> Path | None:
    """The instance workspace the engine runs from (``ROBOTHOR_WORKSPACE``, else ~/robothor)."""
    from robothor.settings import get_settings

    configured = get_settings().paths.workspace.strip()
    if configured:
        return Path(configured).expanduser()
    try:
        return Path.home() / "robothor"
    except RuntimeError:
        return None


def secret_paths(home: Path, workspace: Path | None) -> list[str]:
    """Every path a job may not read, absolute."""
    found = [str(home / rel) for rel in _HOME_SECRETS]
    if workspace is not None:
        found += [str(workspace / rel) for rel in _WORKSPACE_SECRETS]
    found += list(_SYSTEM_SECRETS)
    return list(dict.fromkeys(found))


def allowed_domains() -> tuple[str, ...]:
    """``ROBOTHOR_CODING_ALLOWED_DOMAINS``, comma separated; empty = no network."""
    from robothor.settings import get_settings

    raw = get_settings().coding.allowed_domains
    return tuple(d.strip() for d in raw.replace(";", ",").split(",") if d.strip())


def _git_write_paths(paths: JobPaths) -> tuple[list[str], list[str]]:
    """What ``git commit`` on the job branch writes, and what it must never."""
    common = paths.git_common_dir
    if common is None:
        return [], []
    allow = [str(common / "objects")]
    if paths.branch:
        parent = Path(paths.branch).parent
        allow.append(str(common / "refs" / "heads" / parent))
        allow.append(str(common / "logs" / "refs" / "heads" / parent))
    if (common / "reftable").is_dir():
        allow.append(str(common / "reftable"))
    if paths.git_admin_dir is not None:
        allow.append(str(paths.git_admin_dir))
    deny = [str(common / "hooks"), str(common / "config")]
    return allow, deny


def build_sandbox_settings(
    mode: str, paths: JobPaths, *, allowed_domains: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    """The ``--settings`` object for one job: the sandbox block."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    deny_read = secret_paths(paths.home, paths.workspace)
    if mode == "code":
        git_allow, git_deny = _git_write_paths(paths)
        allow_write = [str(paths.worktree), *git_allow]
        deny_write = git_deny
        domains = [d for d in allowed_domains if d]
    else:
        allow_write = []
        deny_write = [str(paths.worktree)]
        if paths.git_common_dir is not None:
            deny_write.append(str(paths.git_common_dir))
        domains = []
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            # On, a sandboxed Bash command needs no allow rule at all — every
            # command runs (deny rules still win). Right for code mode, whose
            # Bash is unrestricted inside the fence; wrong for the read-only
            # modes, whose git allowlist must stay the gate.
            "autoAllowBashIfSandboxed": mode == "code",
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": deny_read,
                "allowWrite": allow_write,
                "denyWrite": deny_write,
            },
            "network": {"allowedDomains": domains},
        }
    }


def _rule_path(path: str) -> str:
    """``/abs/path`` as a permission-rule path: ``//abs/path``."""
    return "/" + path if path.startswith("/") else path


def _file_rules(tool: str, path: str) -> list[str]:
    p = _rule_path(path)
    return [f"{tool}({p})", f"{tool}({p}/**)"]


def build_permissions(mode: str, paths: JobPaths) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(allowed, disallowed)`` tool rules for ``mode`` over these paths."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    secret_denies: list[str] = []
    for secret in secret_paths(paths.home, paths.workspace):
        for tool in ("Read", "Edit", "Write"):
            secret_denies += _file_rules(tool, secret)
    if mode == "code":
        tree = _rule_path(str(paths.worktree))
        allowed: tuple[str, ...] = (
            "Read",
            "Grep",
            "Glob",
            f"Edit({tree}/**)",
            f"Write({tree}/**)",
            "Bash",
        )
        disallowed = (*NEVER, *secret_denies)
    else:
        allowed = ("Read", "Grep", "Glob", *READ_ONLY_BASH)
        disallowed = (
            "Edit",
            "Write",
            "NotebookEdit",
            *_READ_ONLY_BASH_DENY,
            *NEVER,
            *secret_denies,
        )
    return allowed, tuple(dict.fromkeys(disallowed))


# ── Host prerequisites (the doctor) ───────────────────────────────────

#: What Claude Code's Linux sandbox does on start: bubblewrap in fresh user,
#: pid and network namespaces. If this cannot run, neither can a job's Bash.
_BWRAP_PROBE = (
    "--ro-bind", "/", "/",
    "--dev", "/dev",
    "--unshare-user", "--unshare-pid", "--unshare-net",
    "--die-with-parent",
    "--", "/bin/true",
)  # fmt: skip


def sandbox_problems() -> list[str]:
    """Why Claude Code's Bash sandbox cannot run on this host; empty when it can.

    Every job runs with ``failIfUnavailable``, so any problem here means every
    job would fail at its first shell command.
    """
    problems: list[str] = []
    bwrap = shutil.which("bwrap")
    if not bwrap:
        problems.append("bubblewrap (bwrap) is not installed")
    if not shutil.which("socat"):
        problems.append("socat is not installed (the sandbox's network proxy needs it)")
    if bwrap:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [bwrap, *_BWRAP_PROBE], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"bwrap does not run: {type(exc).__name__}")
        else:
            if proc.returncode != 0:
                why = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
                problems.append(
                    "bwrap cannot create unprivileged user namespaces "
                    f"({why[0][:160]}); on Ubuntu 24.04+ install an AppArmor profile "
                    "for /usr/bin/bwrap that allows `userns`"
                )
    return problems


def unwritable_login_paths(home: Path | None = None) -> list[str]:
    """The host Claude Code login paths this process cannot write.

    Claude Code refreshes its OAuth login under ``~/.claude`` and rewrites
    ``~/.claude.json``; under the engine unit's ``ProtectHome=read-only`` both
    need a ``ReadWritePaths=`` entry (the shipped ``zz-claude-code.conf``).
    """
    base = home if home is not None else Path.home()
    return [
        str(path)
        for path in (base / ".claude", base / ".claude.json")
        if path.exists() and not os.access(path, os.W_OK)
    ]
