"""Which files decide what code the engine imports, so no agent tool writes them.

Everything under ``ROBOTHOR_PLUGIN_DIR`` is on the engine's import path: a
``sitecustomize.py`` there runs at the next engine start, and a
``*.dist-info/entry_points.txt`` naming a ``genus.*`` group is imported on the
next plugin reload. Inside the engine that code holds what the exec scrub keeps
away from agent subprocesses: the database and signing credentials and the
vault key. The plugin lockfile and the files beside it (the grants cache, the
owner marker) decide which of those distributions may load.

The deployment mounts the plugin directory read-only and installs through an
install Job, so this is the second wall, for an engine whose directory is
writable anyway. It follows :mod:`robothor.engine.secret_paths`: the rule is
about PATHS, decided before anything is opened, and an ``exec`` command that
names one of them anywhere is refused unless every part of it only reads.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

__all__ = ["REFUSAL", "exec_writes_managed", "is_managed_path"]

REFUSAL = "Refused: the plugin directory is managed by the install Job; agents cannot write it."

#: Files beside the lockfile that it is read together with.
_LOCK_COMPANIONS = (".grants.json", ".tenant")


def _resolve(path: str | os.PathLike[str]) -> Path:
    """Absolute, ``..`` and symlinks resolved, a missing tail allowed."""
    return Path(path).expanduser().resolve(strict=False)


def _managed() -> tuple[list[Path], list[Path]]:
    """(directories, files) no agent tool may write, resolved."""
    from robothor.plugins.installer import _plugin_target_dir

    dirs: list[Path] = []
    files: list[Path] = []
    plugin_dir = _plugin_target_dir()
    if plugin_dir:
        dirs.append(_resolve(plugin_dir))
    try:
        from robothor.plugins.lockfile import lockfile_path

        lock = lockfile_path()
    except Exception as exc:  # noqa: BLE001 - no lockfile path means none to guard
        logger.debug("Plugin lockfile: path unavailable (%s)", type(exc).__name__)
        lock = None
    if lock is not None:
        resolved = _resolve(lock)
        files.append(resolved)
        files.extend(resolved.with_name(resolved.name + suffix) for suffix in _LOCK_COMPANIONS)
    return dirs, files


def _is_managed(resolved: Path, dirs: list[Path], files: list[Path]) -> bool:
    return resolved in files or any(resolved == d or resolved.is_relative_to(d) for d in dirs)


def is_managed_path(path: str | os.PathLike[str]) -> bool:
    """True when writing *path* would change what the engine imports."""
    dirs, files = _managed()
    if not dirs and not files:
        return False
    return _is_managed(_resolve(path), dirs, files)


# ─── exec ───────────────────────────────────────────────────────────────────

#: Commands that only read, so naming a managed path in them is harmless.
#: Everything else -- copiers, archivers, interpreters, package managers, a
#: verb this list has never heard of -- counts as a writer (fail closed).
_READERS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "stat",
        "file",
        "wc",
        "du",
        "tree",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "sha256sum",
        "sha1sum",
        "md5sum",
        "diff",
        "cmp",
        "realpath",
        "readlink",
        "basename",
        "dirname",
        "echo",
        "printf",
        "test",
        "[",
        "true",
        "false",
        "pwd",
        "find",
        "genus",
    }
)

#: ``find`` actions that change the filesystem.
_FIND_WRITES = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)

#: ``genus`` verbs that write the plugin directory or the lockfile.
_GENUS_PLUGIN_WRITES = frozenset({"install", "remove", "sync", "enable", "disable"})

_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_STRING_LITERAL = re.compile(r"""['"]([^'"\s]+)['"]""")
_GLOB_CHARS = frozenset("*?[")


def _expand_variables(word: str, cwd: str) -> str:
    from robothor.plugins.installer import _plugin_target_dir
    from robothor.settings.env import process_env_get

    def value(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name == "PWD":
            return cwd
        if name == "ROBOTHOR_PLUGIN_DIR":
            return _plugin_target_dir()
        return process_env_get(name, "") or ""

    return _VARIABLE.sub(value, word)


def _candidates(token: str) -> list[str]:
    """The path spellings one shell word may carry."""
    out = [token]
    out.extend(_STRING_LITERAL.findall(token))
    # `of=/x`, `--target=/x`, `a:b`
    for sep in ("=", ":"):
        for word in list(out):
            if sep in word:
                out.extend(part for part in word.split(sep) if part)
    # A redirect glued to its target: `>file`, `2>>file`.
    out.extend(word.lstrip("0123456789&>") for word in list(out) if ">" in word)
    return out


def _glob_names(pattern: str, targets: list[Path]) -> bool:
    """True when a glob could match a managed path or something under one."""
    parts = PurePosixPath(pattern).parts
    for target in targets:
        tparts = target.parts
        n = min(len(parts), len(tparts))
        if n and all(fnmatch.fnmatchcase(tparts[i], parts[i]) for i in range(n)):
            return True
    return False


def _names_managed(
    token: str, bases: list[str], dirs: list[Path], files: list[Path], cwd: str
) -> bool:
    from robothor.engine.secret_paths import _expand_braces

    for raw in _candidates(token):
        expanded = _expand_variables(raw.strip("'\""), cwd)
        if not expanded:
            continue
        for word in _expand_braces(expanded):
            path = Path(word).expanduser()
            spellings = [path] if path.is_absolute() else [Path(b) / path for b in bases]
            for spelled in spellings:
                normal = os.path.normpath(spelled)
                if any(ch in normal for ch in _GLOB_CHARS):
                    if _glob_names(normal, [*dirs, *files]):
                        return True
                    continue
                if _is_managed(_resolve(normal), dirs, files):
                    return True
    return False


def _only_reads(word: str | None, args: list[str]) -> bool:
    if word is None:
        return True
    if word not in _READERS:
        return False
    if word == "find":
        return not any(a in _FIND_WRITES for a in args)
    if word == "genus":
        return not (args[:1] == ["plugin"] and any(a in _GENUS_PLUGIN_WRITES for a in args[1:]))
    return True


def exec_writes_managed(command: str, *, cwd: str | None = None) -> str | None:
    """The refusal for a shell command that may write a managed path, else None.

    A command is refused when any word in it names the plugin directory (or
    something under it) or a lockfile, through a relative path, a ``cd``, a
    variable, a brace or a glob, AND any part of it writes: an output
    redirect, or a command that is not a known reader.
    """
    dirs, files = _managed()
    if not dirs and not files:
        return None
    from robothor.engine.secret_paths import (
        _changed_directories,
        _command_word,
        _segments,
    )

    shell_cwd = cwd or str(Path.cwd())
    segments = _segments(command)
    bases = [shell_cwd]
    for target in _changed_directories(segments):
        expanded = Path(_expand_variables(target, shell_cwd)).expanduser()
        bases.extend([os.path.normpath(Path(base) / expanded) for base in bases])

    named = any(
        _names_managed(tok, bases, dirs, files, shell_cwd) for seg in segments for tok in seg
    )
    if not named:
        return None
    # A redirect's target is split off as a segment of its own, so it reads as
    # a command word no reader has: `> file` counts as a write by itself. The
    # sinks that cannot hold a file (`2>/dev/null`, `2>&1`) are not one.
    writes = not all(_only_reads(*_command_word(seg)) for seg in segments if not _is_sink(seg))
    return REFUSAL if writes else None


def _is_sink(segment: list[str]) -> bool:
    return len(segment) == 1 and (segment[0] in _SINKS or segment[0].isdigit())


_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
