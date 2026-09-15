"""Writing one setting into ``config.yaml``'s ``settings:`` block.

Two callers have to do this: ``genus config set``, and the first-run wizard's
operator step, which turns ``GENUS_LOCAL_LOGIN`` on so the account it just
created can sign in. A second implementation would have been a second opinion
about what that file means — indentation, an existing key written under a
deprecated spelling, a trailing comment, the atomic replace, the file mode —
and the failure mode is a surface reporting a setting applied while the
service reads something else. So the machinery lives here and both callers go
through :func:`write_setting`.

Deliberately free of ``pydantic_settings``: ``robothor.cli`` imports this
module at import time, and ``tests/test_settings_registry.py`` asserts in a
subprocess that ``genus --help`` does not pay for pydantic. Resolving WHERE
config.yaml is stays in :mod:`robothor.settings.sources`; this module is handed
the path.

The edit is TEXTUAL, not a load-and-dump round trip. PyYAML is the only YAML
library this platform ships, it does not preserve comments, and config.yaml is
a file operators hand-edit — so a round trip would silently delete every
comment in it every time a setting changed.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Sequence

__all__ = ["NEW_FILE_MODE", "write_setting", "write_settings", "write_top_level"]

#: One change: ``(group, field, value, other spellings the field answers to)``.
Change = tuple[str, str, Any, "tuple[str, ...]"]

#: Serialises read-splice-replace within this process. The bridge's settings
#: handlers are plain ``def``, so FastAPI runs them in its worker threadpool --
#: genuinely parallel. Two requests that both read before either replaces
#: produce a lost update, and the losing one still reports the change applied:
#: precisely the "reports applied and changes nothing" failure this package
#: exists to end. Process-wide because the path is resolved per call and two
#: writers to one file must queue whichever way they spelled it.
_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold the write lock for ``path`` across read, splice and replace.

    Two locks, because there are two kinds of concurrent writer:

    * the in-process :data:`_WRITE_LOCK`, for two bridge requests in the
      threadpool;
    * an ``flock`` on the containing DIRECTORY, for the bridge racing a
      ``genus config set`` or the first-run wizard in another process. The
      directory rather than a sidecar file: the config file's identity is
      replaced by the rename, so a lock held on it would not be the lock the
      next writer takes -- and a sidecar would be a file in the operator's
      ``.robothor/`` that nothing else explains.

    ``fcntl`` is POSIX-only. Every supported deployment is Linux (systemd
    units throughout), and where it is missing the in-process lock still
    holds, which is the race that is actually reachable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not reachable on Linux
            yield
            return
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def write_setting(
    group: str,
    field: str,
    value: Any,
    *,
    names: tuple[str, ...] = (),
    path: Path,
) -> Path:
    """Set ``settings.<group>.<field>`` in ``path`` and change nothing else.

    ``names`` is every other spelling the field answers to — its declared
    environment name and any deprecated alias. An existing key under one of
    those is updated IN PLACE, keeping the operator's spelling: writing the
    Python name beside it would leave one field configured twice in one
    mapping, which is a file whose meaning depends on which key pydantic reads
    last.

    Returns the path written. Raises ``OSError`` and writes nothing on a
    failure — the replace is atomic, so a full disk or a Ctrl-C leaves the
    operator with the file they had rather than half of a new one.
    """
    return write_settings([(group, field, value, names)], path=path)


def write_settings(changes: Sequence[Change], *, path: Path) -> Path:
    """Set several settings in ONE read, splice and atomic replace.

    A loop over :func:`write_setting` is atomic per FIELD, not per request: a
    failure on the second field leaves the first one written, and the caller
    that asked for a batch has no way to say what the instance is now
    configured to do. One splice of the whole set and one ``os.replace`` makes
    the batch the unit, which is what an operator saving a form means by it.

    Every change is applied to the same text in order, so two changes to one
    field resolve last-one-wins exactly as they would in a mapping.

    Returns the path written. Raises ``OSError`` and writes nothing on any
    failure, including a value that does not read back as itself
    (see :func:`_verify_round_trip`).
    """
    with _exclusive(path):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # No file yet (or an unreadable one): _splice builds the block from
            # scratch. An unreadable-but-present file would fail again on write,
            # which is where the operator gets the error.
            text = ""
        for group, field, value, names in changes:
            text = _splice(text, group, field, _render(value), names)
        _verify_round_trip(text, changes, path)
        _write_atomically(path, text)
    return path


def _verify_round_trip(text: str, changes: Sequence[Change], path: Path) -> None:
    """Refuse to replace the file unless it parses and says what we meant.

    The guard is the round trip itself rather than a list of characters to
    quote — that list existed, was wrong (a YAML block-sequence indicator is
    ``-`` followed by a space, and ``-`` was not on it), and a corrected list
    would be wrong again the next time somebody typed a construct nobody had
    thought of. What an operator is owed is not a clever quoter: it is that a
    surface reporting "applied" never leaves behind a config.yaml the instance
    cannot start on.

    Raises ``OSError`` — the class every caller of this module already handles
    as "the write failed, the file is unchanged".
    """
    import yaml

    try:
        loaded = yaml.safe_load(text) or {}
        block = (loaded.get("settings") or {}) if isinstance(loaded, dict) else {}
    except yaml.YAMLError as exc:
        raise OSError(f"refusing to write {path}: the result is not valid YAML ({exc})") from exc

    for group, field, value, names in changes:
        mapping = block.get(group) if isinstance(block, dict) else None
        if not isinstance(mapping, dict):
            raise OSError(f"refusing to write {path}: {group}.{field} did not survive the edit")
        for key in (field, *names):
            if key in mapping:
                if mapping[key] != value:
                    raise OSError(
                        f"refusing to write {path}: {group}.{field} would read back as "
                        f"{mapping[key]!r}, not what was set"
                    )
                break
        else:
            raise OSError(f"refusing to write {path}: {group}.{field} did not survive the edit")


def write_top_level(key: str, value: Any, *, path: Path) -> Path:
    """Set a TOP-LEVEL key in config.yaml, outside the ``settings:`` block.

    For notes that are not settings — ``setup_completed_at`` is the first — and
    the distinction is load-bearing, not tidiness. The ``settings:`` block is
    validated against the registry, and under ``config_strict_mode: enforce``
    an undeclared key in it is REJECTED by name: a marker written there would
    make a freshly-completed instance refuse to start. Everything outside that
    block is explicitly not the settings model's to validate (federation
    identity already lives there).

    Same textual edit, same atomic replace and the SAME LOCK as
    :func:`write_settings`, so an operator's comments and the file's mode
    survive — and so the first-run wizard writing ``setup_completed_at`` cannot
    race a settings write on the same file and drop one of the two. They edit
    different regions of one document through one read-modify-write each; the
    lock is what makes "different regions" true rather than lucky.
    """
    if not key or ":" in key or key.strip() != key:
        raise ValueError(f"{key!r} is not a usable top-level key")

    with _exclusive(path):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""

        rendered = _render(value)
        lines = text.splitlines()
        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if _indent_of(raw) == 0 and stripped.split(":", 1)[0] == key and ":" in stripped:
                lines[index] = f"{key}: {rendered}{_inline_comment(stripped)}"
                _write_atomically(path, "\n".join(lines) + "\n")
                return path

        # Appended rather than prepended: a leading comment block in a
        # hand-written file is the first thing an operator reads, and a machine
        # line above it moves their own header down the page on every write.
        prefix = lines + ([""] if lines and lines[-1].strip() else [])
        _write_atomically(path, "\n".join([*prefix, f"{key}: {rendered}"]) + "\n")
    return path


def _render(value: Any) -> str:
    """One YAML scalar, quoted exactly when PyYAML says it has to be.

    Emitted by the same library the platform LOADS this file with, so the two
    cannot disagree about what a scalar means. The rule this replaces was a
    hand-written list of characters to quote, and it was wrong: a YAML
    block-sequence indicator is ``-`` followed by a space, ``-`` was not on the
    list, and ``log_dir: - item`` is a file the instance cannot parse. A list
    like that is only ever one construct away from being wrong again; a dumper
    is not.

    Plain values stay plain — ``/var/log/robothor``, ``3``, ``true`` render
    exactly as they did — because that is what PyYAML emits for them too.

    A value containing a newline is forced to double-quoted style: single
    quotes cannot hold one on a single line, and :func:`_splice` writes one
    line.
    """
    import yaml

    style = '"' if isinstance(value, str) and ("\n" in value or "\r" in value) else None
    text = yaml.safe_dump(
        value,
        default_flow_style=True,
        default_style=style,
        width=10**9,
        allow_unicode=True,
    ).rstrip("\n")
    # A plain top-level scalar is emitted as its own document and gets an
    # explicit end marker. The scalar is what the caller asked for.
    text = text.removesuffix("\n...")
    return text.rstrip("\n")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _comment_at(text: str) -> int:
    """Index of the ``#`` that starts a YAML comment in ``text``, or -1.

    A ``#`` only opens a comment at the start of the scanned text or after
    whitespace: ``tag: a#b`` is the three-character value ``a#b``.
    """
    match = re.search(r"(?:^|\s)#", text)
    return -1 if match is None else match.end() - 1


def _inline_comment(stripped: str) -> str:
    """Any trailing ``# ...`` on a key line, as text to re-append.

    A ``#`` inside a quoted value is part of the value, and a comment can
    still follow the closing quote -- ``ai_name: "Ada"  # the boss`` is both
    at once. Treating the whole line as uncommentable dropped the operator's
    comment; treating the first ``#`` as the comment would have moved half
    their value into one. So a quoted value is scanned to its closing quote
    and only what follows is searched.

    An unterminated quote (a line a YAML parser would reject anyway) yields no
    comment: the write is going to replace the value, and inventing a comment
    boundary inside a broken string is the one outcome worse than losing it.
    """
    _key, _, value = stripped.partition(":")
    rest = value.lstrip()
    if rest[:1] not in {'"', "'"}:
        found = _comment_at(rest)
        return "" if found < 0 else "  " + rest[found:].rstrip()

    quote = rest[0]
    index = 1
    while index < len(rest):
        char = rest[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            if quote == "'" and rest[index + 1 : index + 2] == "'":
                index += 2  # '' is an escaped single quote, not the end
                continue
            break
        index += 1
    else:
        return ""
    tail = rest[index + 1 :]
    found = _comment_at(tail)
    return "" if found < 0 else "  " + tail[found:].rstrip()


def _splice(text: str, group: str, field: str, rendered: str, names: tuple[str, ...] = ()) -> str:
    """Set ``settings.<group>.<field>`` in ``text``, touching nothing else.

    A load-and-dump round trip through PyYAML (the only YAML library this
    platform ships -- ruamel is not a dependency, and adding one to change a
    single scalar is not a trade worth making) would reformat the file and
    delete every comment in it. config.yaml is a file operators hand-edit, so
    the edit is textual: find the line, replace the value, leave the rest of
    the bytes exactly as they were.

    ``names`` is every other spelling the field answers to -- its declared
    environment name and any deprecated alias, all legal keys here because the
    groups are ``populate_by_name``. An existing key under one of those is
    UPDATED IN PLACE, keeping the operator's spelling: writing the Python name
    beside it would leave one field configured twice in one mapping, which is
    a file whose meaning depends on which key pydantic reads last.
    """
    lines = text.splitlines()
    line = f"{field}: {rendered}"
    spellings = (field, *names)

    # 1. the settings: block
    start = next(
        (i for i, raw in enumerate(lines) if raw.rstrip() == "settings:" and _indent_of(raw) == 0),
        None,
    )
    if start is None:
        prefix = lines + ([""] if lines and lines[-1].strip() else [])
        return "\n".join([*prefix, "settings:", f"  {group}:", f"    {line}"]) + "\n"

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and _indent_of(lines[i]) == 0:
            end = i
            break

    body = [i for i in range(start + 1, end) if lines[i].strip()]
    step = _indent_of(lines[body[0]]) if body else 2

    # 2. the group within it
    header = next(
        (
            i
            for i in body
            if _indent_of(lines[i]) == step
            and lines[i].strip().rstrip(":") == group
            and lines[i].strip().endswith(":")
        ),
        None,
    )
    if header is None:
        insert = end
        while insert > start + 1 and not lines[insert - 1].strip():
            insert -= 1
        return (
            "\n".join(
                [
                    *lines[:insert],
                    f"{' ' * step}{group}:",
                    f"{' ' * step * 2}{line}",
                    *lines[insert:],
                ]
            )
            + "\n"
        )

    group_end = end
    for i in range(header + 1, end):
        if lines[i].strip() and _indent_of(lines[i]) <= step:
            group_end = i
            break
    inner = [i for i in range(header + 1, group_end) if lines[i].strip()]
    field_indent = _indent_of(lines[inner[0]]) if inner else step * 2

    # 3. the field within the group, under any spelling it answers to
    for i in inner:
        stripped = lines[i].strip()
        key = stripped.split(":", 1)[0]
        if _indent_of(lines[i]) == field_indent and key in spellings:
            lines[i] = f"{' ' * field_indent}{key}: {rendered}{_inline_comment(stripped)}"
            return "\n".join(lines) + "\n"

    insert = group_end
    while insert > header + 1 and not lines[insert - 1].strip():
        insert -= 1
    return "\n".join([*lines[:insert], f"{' ' * field_indent}{line}", *lines[insert:]]) + "\n"


#: Mode for a config.yaml this command creates. The file records how the
#: instance is wired -- ports, hosts, endpoints -- and nothing else on the box
#: needs to read it, so a new one starts private to the operator.
NEW_FILE_MODE = 0o600


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, or not at all.

    A half-written config.yaml is a box that will not start, and the write can
    be interrupted (a full disk, a reboot, a Ctrl-C). Same directory, so the
    rename cannot cross a filesystem boundary and stop being atomic.

    Rename replaces the file's identity, not just its contents, so the mode and
    the owner have to be carried across deliberately: a temp file takes the
    process umask and the process's own uid, and ``sudo genus config set``
    would otherwise hand the engine's config file to root and leave the service
    unable to write it again. An existing file keeps exactly the mode and
    owner it had; a new one is created private (:data:`NEW_FILE_MODE`).
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing: os.stat_result | None = path.stat()
    except OSError:
        existing = None

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config.yaml.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(NEW_FILE_MODE if existing is None else existing.st_mode & 0o7777)
        if existing is not None and os.geteuid() == 0:
            # Only root can give a file away; anyone else already owns it.
            os.chown(tmp, existing.st_uid, existing.st_gid)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
