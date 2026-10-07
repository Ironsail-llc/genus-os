"""Which systemd file sets a setting -- and changing it in that layer.

``genus config get`` resolves the environment of the process that runs it, and
for a value a systemd drop-in injected it could only say ``source: env``. An
agent blocked by a setting could not name the file that set it, and the shell
it runs in (``robothor-host-exec``) does not even carry the engine's drop-ins,
so it could not see the value at all. This module answers both questions from
the unit files themselves:

* :func:`lookup` asks ``systemctl show`` for a unit's fragment and drop-in
  paths (in the order systemd applies them), reads them, and
  :func:`find_origin` walks the ``[Service]`` section the way systemd does:
  later ``Environment=`` assignments replace earlier ones, an empty
  ``Environment=`` resets the list, and any ``EnvironmentFile=`` value beats
  every ``Environment=`` line.
* :func:`plan_dropin` / :func:`apply_dropins` write the operator's change as a
  drop-in that sorts after the current winner, reload systemd and schedule the
  restart through ``systemd-run --on-active=15s`` -- so the run that made the
  change can finish its reply before its own engine restarts.

Values read from an ``EnvironmentFile=`` are never surfaced (that file is
usually the secrets file); values from ``Environment=`` lines are shown only
through :func:`describe`, which masks a declared secret and anything the
platform's redactor reads as a credential.

Every subprocess and file read is injectable; nothing here reads /etc or runs
a command at import time. Stdlib only at import, so ``genus --help`` stays
cheap (``tests/test_settings_registry.py``).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "ApplyError",
    "ApplyResult",
    "DropinConflictError",
    "DropinPlan",
    "Origin",
    "apply_dropins",
    "describe",
    "dropin_basename",
    "find_origin",
    "lookup",
    "parse_env_file",
    "parse_environment_value",
    "parse_show",
    "plan_dropin",
    "unit_name",
]

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Reader = Callable[[str], "str | None"]

#: How long the scheduled restart waits. Long enough for the run that made the
#: change to post its reply; short enough that the operator sees it take.
RESTART_DELAY = "15s"


class DropinConflictError(Exception):
    """The change cannot win from the default drop-in name; the message says why."""


class ApplyError(Exception):
    """A step of writing / reloading / scheduling failed; the message names it."""


@dataclass
class Origin:
    """Where one variable's value for one unit comes from."""

    unit: str
    name: str
    kind: str  # "Environment" or "EnvironmentFile"
    path: str  # the unit file with the line, or the environment file
    line: int | None = None
    value: str | None = None
    declared_in: str | None = None  # for EnvironmentFile: the unit file naming it
    shadowed: list[Origin] = field(default_factory=list)
    #: Environment files this process could not read (a root-only secrets
    #: file). An environment file beats every Environment= line, so the
    #: variable MAY come from one of these instead; said, not guessed.
    unreadable: list[str] = field(default_factory=list)


@dataclass
class DropinPlan:
    unit: str
    name: str
    path: str
    content: str


@dataclass
class ApplyResult:
    written: list[str]
    units: list[str]
    restart_unit: str


def unit_name(unit: str) -> str:
    return unit if "." in unit else f"{unit}.service"


def _default_runner(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=30)


def _default_reader(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ── parsing ──────────────────────────────────────────────────────────────────


def parse_environment_value(value: str) -> list[tuple[str, str]]:
    """The ``NAME=value`` pairs of one ``Environment=`` line, quotes honoured."""
    try:
        words = shlex.split(value, posix=True)
    except ValueError:
        words = value.split()
    pairs = []
    for word in words:
        name, sep, val = word.partition("=")
        if sep and name:
            pairs.append((name, val))
    return pairs


def parse_env_file(text: str) -> dict[str, str]:
    """``EnvironmentFile=`` syntax: ``NAME=value`` lines, ``#``/``;`` comments."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name or " " in name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    return out


def _service_directives(text: str) -> list[tuple[int, str, str]]:
    """``(line number, key, value)`` for each directive in ``[Service]``."""
    out = []
    section = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Service":
            continue
        key, sep, value = line.partition("=")
        if sep:
            out.append((number, key.strip(), value.strip()))
    return out


def find_origin(
    name: str,
    files: list[tuple[str, str]],
    *,
    read_env_file: Reader,
    unit: str = "",
    unreadable: list[str] | None = None,
) -> Origin | None:
    """The winning source of ``name`` across ``files`` (fragment, then drop-ins).

    ``files`` must be in the order systemd applies them -- ``parse_show`` of
    ``systemctl show -p FragmentPath,DropInPaths`` gives exactly that.
    ``unreadable`` collects environment files that could not be read (a
    root-only secrets file): the variable may be set there, and saying so is
    better than guessing.
    """
    assignments: list[Origin] = []
    env_files: list[tuple[str, str]] = []  # (path, declared_in)
    for path, text in files:
        for number, key, value in _service_directives(text):
            if key == "Environment":
                if not value:
                    assignments = []
                    continue
                for var, val in parse_environment_value(value):
                    if var == name:
                        assignments.append(
                            Origin(
                                unit=unit,
                                name=name,
                                kind="Environment",
                                path=path,
                                line=number,
                                value=val,
                            )
                        )
            elif key == "EnvironmentFile":
                if not value:
                    env_files = []
                    continue
                env_files.append((value.lstrip("-"), path))

    file_hit: Origin | None = None
    for env_path, declared_in in env_files:
        env_text = read_env_file(env_path)
        if env_text is None:
            if unreadable is not None:
                unreadable.append(env_path)
            continue
        if name in parse_env_file(env_text):
            # Later files override earlier ones; the value is never kept.
            file_hit = Origin(
                unit=unit, name=name, kind="EnvironmentFile", path=env_path, declared_in=declared_in
            )

    winner = file_hit or (assignments[-1] if assignments else None)
    if winner is None:
        return None
    winner.shadowed = [o for o in assignments if o is not winner]
    return winner


def parse_show(output: str) -> list[str]:
    """Fragment then drop-in paths, from ``systemctl show -p FragmentPath,DropInPaths``."""
    fragment: list[str] = []
    dropins: list[str] = []
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key == "FragmentPath" and value.strip():
            fragment = [value.strip()]
        elif key == "DropInPaths":
            dropins = value.split()
    return fragment + dropins


def lookup(
    name: str,
    units: list[str] | tuple[str, ...],
    *,
    runner: Runner | None = None,
    reader: Reader | None = None,
) -> list[Origin]:
    """One :class:`Origin` per unit that sets ``name``. Empty when systemd is absent."""
    run = runner or _default_runner
    read = reader or _default_reader
    origins: list[Origin] = []
    for raw in units:
        unit = unit_name(raw)
        try:
            shown = run(["systemctl", "show", unit, "-p", "FragmentPath,DropInPaths"])
        except (OSError, subprocess.SubprocessError):
            return []
        if shown.returncode != 0:
            continue
        files = [(p, text) for p in parse_show(shown.stdout) if (text := read(p)) is not None]
        unreadable: list[str] = []
        origin = find_origin(name, files, read_env_file=read, unit=unit, unreadable=unreadable)
        if origin is not None:
            origin.unreadable = unreadable
            origins.append(origin)
    return origins


# ── showing an origin ────────────────────────────────────────────────────────


def _looks_secret(name: str, value: str) -> bool:
    from robothor.secrets.redaction import redact

    probe = f"{name}={value}"
    return redact(probe) != probe


def describe(origin: Origin, *, secret: bool) -> str:
    """One line for a terminal. Never a credential, never an env-file value."""
    if origin.kind == "EnvironmentFile":
        return (
            f"{origin.unit}: EnvironmentFile {origin.path} "
            f"(named in {origin.declared_in}); value not shown"
        )
    value = origin.value or ""
    if secret or _looks_secret(origin.name, value):
        from robothor.secrets.fingerprint import fingerprint

        shown = f"<set, {fingerprint(value)}>" if value else "<unset>"
    else:
        shown = value
    return f"{origin.unit}: {origin.name}={shown} from {origin.path}:{origin.line}"


# ── writing a drop-in ────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def dropin_basename(name: str, zs: int = 2) -> str:
    return f"{'z' * zs}-genus-config-{_slug(name)}.conf"


_OWN = re.compile(r"^z+-genus-config-(?P<slug>[a-z0-9-]+)\.conf$")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def plan_dropin(
    name: str,
    value: str,
    unit: str,
    origin: Origin | None,
    *,
    override: bool = False,
    note: str = "",
) -> DropinPlan:
    """The drop-in that makes ``name=value`` the unit's effective value.

    Raises:
        DropinConflictError: the variable comes from an ``EnvironmentFile=`` (which
            beats every ``Environment=`` line, so no drop-in can win), or a
            drop-in that sorts after the default name sets it and ``override``
            was not given. The message names the file.
    """
    unit = unit_name(unit)
    directory = f"/etc/systemd/system/{unit}.d"
    basename = dropin_basename(name)

    if origin is not None and origin.kind == "EnvironmentFile":
        raise DropinConflictError(
            f"{name} for {unit} comes from EnvironmentFile {origin.path} (named in "
            f"{origin.declared_in}). systemd lets an environment file beat every "
            f"Environment= line, so a drop-in cannot change it: edit {origin.path} "
            "or remove the variable from it, then restart."
        )

    # A line in the unit fragment itself is beaten by any drop-in; only a
    # drop-in competes on its name.
    if (
        origin is not None
        and origin.kind == "Environment"
        and PurePosixPath(origin.path).parent.name.endswith(".d")
    ):
        winner = PurePosixPath(origin.path)
        own = _OWN.match(winner.name)
        if own and own.group("slug") == _slug(name):
            basename = winner.name  # our own drop-in already wins; rewrite it
            directory = str(winner.parent)
        elif winner.name > basename:
            if not override:
                raise DropinConflictError(
                    f"{name} for {unit} is set by {origin.path}:{origin.line}, which "
                    f"sorts after {basename} and would keep winning. Re-run with "
                    "--override to write a drop-in that sorts after it, or edit "
                    f"{origin.path}."
                )
            zs = len(winner.name) - len(winner.name.lstrip("z")) + 1
            basename = dropin_basename(name, zs)
            while basename <= winner.name:  # pragma: no cover - belt and braces
                zs += 1
                basename = dropin_basename(name, zs)

    header = [
        "# Written by `genus config set --apply`; re-run it to change this value.",
        f"# Setting: {name}",
    ]
    if note:
        header.append(f"# {note}")
    if origin is not None:
        header.append(f"# Previously from: {origin.path}:{origin.line}")
    content = "\n".join([*header, "[Service]", f'Environment="{name}={_escape(value)}"', ""])
    return DropinPlan(unit=unit, name=name, path=f"{directory}/{basename}", content=content)


def apply_dropins(
    plans: list[DropinPlan],
    *,
    runner: Runner | None = None,
    scratch: Path | None = None,
    stamp: str,
    reload: bool = True,
    units: list[str] | None = None,
) -> ApplyResult:
    """Install each drop-in with sudo, reload systemd, schedule the restart.

    ``units`` adds units to restart beyond those the plans write to (a
    config.yaml change that still needs its declared units restarted). The
    restart is a transient timer (``systemd-run --on-active``), so it runs
    outside the caller's own cgroup and survives the engine it restarts.

    Raises:
        ApplyError: a step failed; nothing after it ran.
    """
    run = runner or _default_runner
    units = sorted({p.unit for p in plans} | set(units or ()))

    def step(argv: list[str]) -> None:
        try:
            done = run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApplyError(f"{' '.join(argv)}: {exc}") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()
            raise ApplyError(f"{' '.join(argv)} failed ({done.returncode}): {detail}")

    written: list[str] = []
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        for index, plan in enumerate(plans):
            source = Path(tmp) / f"dropin-{index}.conf"
            source.write_text(plan.content, encoding="utf-8")
            step(["sudo", "-n", "install", "-D", "-m", "0644", str(source), plan.path])
            written.append(plan.path)
    if reload:
        step(["sudo", "-n", "systemctl", "daemon-reload"])
    restart_unit = f"genus-config-restart-{stamp}"
    step(
        [
            "sudo",
            "-n",
            "systemd-run",
            f"--on-active={RESTART_DELAY}",
            f"--unit={restart_unit}",
            "systemctl",
            "restart",
            *units,
        ]
    )
    return ApplyResult(written=written, units=units, restart_unit=restart_unit)
