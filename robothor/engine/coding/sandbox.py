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
  Claude Code applies a ``Read`` deny rule to Grep and Glob as well: probed
  against the CLI, a Grep or Glob aimed at a denied directory or file is
  refused, and one over its parent leaves the denied entries out of the
  results, so no separate Grep/Glob rule is written.

The secret paths are fixed ones (credentials under the home, the instance's
own state, ``/run/robothor``, ``/etc/robothor`` and the chart's credential
mounts under ``/etc/genus``) plus whatever the engine's settings point at: the
Google Workspace CLI's credential file, its directory and its token-cache
directory, and the vault key, fingerprint salt and secrets env files.

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

from robothor.engine.coding.env import job_homes_root
from robothor.engine.coding.runner import MODES

__all__ = [
    "JobPaths",
    "allowed_domains",
    "build_permissions",
    "build_sandbox_settings",
    "engine_unit_proc_problems",
    "live_workspace",
    "required",
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

#: Under the live workspace: agent memory, instance config, and the vault's
#: master key and fingerprint salt where no setting moves them.
_WORKSPACE_SECRETS: tuple[str, ...] = (
    "brain",
    ".robothor",
    "local",
    ".vault-key",
    ".fingerprint-salt",
)

#: System-wide: decrypted secrets (tmpfs), the instance env file, and the Helm
#: chart's read-only credential mounts (``/etc/genus``: the vault master key and
#: salt, the Google Workspace OAuth file, the database CA).
_SYSTEM_SECRETS: tuple[str, ...] = ("/run/robothor", "/etc/robothor", "/etc/genus")

#: Settings naming a credential FILE: ``(group, field)``. The file is denied.
_CREDENTIAL_FILE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("channels", "google_workspace_credentials_file"),
    ("secrets", "vault_key_file"),
    ("secrets", "fingerprint_salt_file"),
    ("secrets", "env_file"),
    ("secrets", "backend_file"),
    ("ops", "secrets_file"),
    ("ops", "instance_env"),
)

#: Settings naming a credential DIRECTORY (the Workspace CLI's token cache).
#: The Workspace credential file's own directory is denied with it.
_CREDENTIAL_DIR_SETTINGS: tuple[tuple[str, str], ...] = (
    ("channels", "google_workspace_config_dir"),
)

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


def required() -> bool:
    """False only when ``ROBOTHOR_CODING_SANDBOX`` is ``off``; anything else fails closed."""
    from robothor.settings import get_settings

    return str(get_settings().coding.sandbox or "").strip().lower() != "off"


def _configured(group: str, field: str) -> Path | None:
    from robothor.settings import get_settings

    raw = str(getattr(getattr(get_settings(), group), field) or "").strip()
    return Path(raw).expanduser() if raw else None


def _dir_is_deniable(directory: Path, keep: tuple[Path, ...]) -> bool:
    """False for ``/`` and any directory that is or holds one of ``keep`` (the
    home, the workspace, the coding workspace, the job's worktree and git
    directory): denying one of those would take away the job's own files."""
    if directory == Path(directory.anchor):
        return False
    return not any(directory == k or directory in k.parents for k in keep)


def _configured_secret_paths(keep: tuple[Path, ...]) -> list[str]:
    """The credential files and directories the engine's settings name.

    A credential file's directory is incidental, so it is also spared when it
    is top-level (``/etc``, ``/tmp``) and only the file is denied. A configured
    credential directory IS the credential and has no file to fall back on, so
    it is denied even top-level.
    """
    found: list[str] = []
    for group, field in _CREDENTIAL_FILE_SETTINGS:
        path = _configured(group, field)
        if path is not None:
            found.append(str(path))
    gws_file = _configured("channels", "google_workspace_credentials_file")
    if gws_file is not None:
        parent = gws_file.parent
        if parent.parent != Path(parent.anchor) and _dir_is_deniable(parent, keep):
            found.append(str(parent))
    for group, field in _CREDENTIAL_DIR_SETTINGS:
        directory = _configured(group, field)
        if directory is not None and _dir_is_deniable(directory, keep):
            found.append(str(directory))
    return found


def secret_paths(
    home: Path, workspace: Path | None, *, keep: tuple[Path | None, ...] = ()
) -> list[str]:
    """Every path a job may not read, absolute. ``keep``: the job's own paths,
    which no setting's directory may swallow."""
    found = [str(home / rel) for rel in _HOME_SECRETS]
    if workspace is not None:
        found += [str(workspace / rel) for rel in _WORKSPACE_SECRETS]
    homes = job_homes_root()
    if homes is not None:
        found.append(str(homes))  # every job's private home, its own included
    found += list(_SYSTEM_SECRETS)
    kept = (home, workspace, homes.parent if homes is not None else None, *keep)
    found += _configured_secret_paths(tuple(k for k in kept if k is not None))
    return list(dict.fromkeys(found))


def _job_secret_paths(paths: JobPaths) -> list[str]:
    return secret_paths(paths.home, paths.workspace, keep=(paths.worktree, paths.git_common_dir))


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
    deny_read = _job_secret_paths(paths)
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
    for secret in _job_secret_paths(paths):
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
#: pid and network namespaces, with a fresh /proc for the new pid namespace.
#: If this cannot run, neither can a job's Bash. The /proc mount is the part
#: systemd hardening breaks (see _PROC_OVERMOUNTS), so the probe must make it:
#: without ``--proc`` it passed inside the engine unit while every job's Bash
#: failed "Can't mount proc" (observed 2026-10-05).
_BWRAP_PROBE = (
    "--ro-bind", "/", "/",
    "--dev", "/dev",
    "--proc", "/proc",
    "--unshare-user", "--unshare-pid", "--unshare-net",
    "--die-with-parent",
    "--", "/bin/true",
)  # fmt: skip

#: Engine-unit directives that overmount parts of /proc. The kernel refuses a
#: fresh /proc mount inside a user namespace while /proc carries overmounts,
#: so either one breaks Claude Code's sandbox. ProtectProc=, ProtectClock=,
#: ProtectHostname=, ProtectControlGroups= and an empty CapabilityBoundingSet=
#: do not. zz-claude-code.conf turns these two off for the engine.
_PROC_OVERMOUNTS = ("ProtectKernelTunables", "ProtectKernelLogs")
_ENGINE_UNIT = "robothor-engine.service"
_PROC_FIX = (
    "the engine unit's ProtectKernelTunables=/ProtectKernelLogs= overmount /proc, "
    "so bubblewrap cannot mount a fresh one: install "
    "infra/systemd/robothor-engine.service.d/zz-claude-code.conf (sets both to no) "
    "with scripts/install-units.sh"
)


def _show_engine_unit() -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["systemctl", "show", _ENGINE_UNIT, *(f"--property={p}" for p in _PROC_OVERMOUNTS)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


def engine_unit_proc_problems(show: Any = None) -> list[str]:
    """The engine unit's /proc overmounts, read from systemd; empty when none.

    ``robothor doctor`` runs from a shell, not inside the engine unit, so its
    own bwrap probe cannot see what the unit's hardening does to /proc. The
    unit's effective properties can. No systemd, or no engine unit installed,
    is nothing to report.
    """
    try:
        shown = (show or _show_engine_unit)()
    except (OSError, subprocess.SubprocessError):
        return []
    enabled = [
        key
        for key, _, value in (line.partition("=") for line in shown.splitlines())
        if key in _PROC_OVERMOUNTS and value.strip() == "yes"
    ]
    if not enabled:
        return []
    return [f"{', '.join(f'{k}=yes' for k in enabled)} on {_ENGINE_UNIT}: {_PROC_FIX}"]


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
                # "Can't mount proc on /newroot/proc"; a userns refusal names
                # /proc/self/uid_map instead, so match the mount, not "proc".
                if "mount proc" in why[0].lower():
                    problems.append(f"bwrap cannot mount /proc ({why[0][:160]}); {_PROC_FIX}")
                else:
                    problems.append(
                        "bwrap cannot create unprivileged user namespaces "
                        f"({why[0][:160]}); on Ubuntu 24.04+ install an AppArmor profile "
                        "for /usr/bin/bwrap that allows `userns`"
                    )
    problems.extend(engine_unit_proc_problems())
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
