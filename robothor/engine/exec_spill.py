"""What `exec` shows the model, and where the rest of it goes.

#583 replaced a bare ``proc.stdout[:4000]`` with a marker that NAMES the cut.
That is necessary and it is not sufficient: the other 8,431 characters were
still destroyed at the handler, so an agent told "the rest is missing" had no
way to go and get it, and the only remedy on offer was to run the command
again and hope it printed less.

MEASURED 2026-09-16, the worst-scoring Social task. One listing call returned
twenty records inside ``{"records": [...], "total": 20}``. The slice landed in
record twelve, and ``total`` — the one field that would have exposed the cut —
sat *after* the array, in the tail. The agent read the twelve records it could
see, reported "all 12", and every graded item past the cut scored zero while
every item inside the window scored full marks.

So the rule here is the one ``code_exec_result`` already states for the sibling
tool: **truncation is pagination, not amputation.** The model reads a bounded
head; the whole stream goes to a file under the workspace; the marker names the
path; ``read_file`` gets it back. The cap itself does not move — a bigger slice
has the same defect one order of magnitude later.

Where the file goes, and why there:

* under the **workspace**, so the agent can ``read_file`` it and so nothing
  lands in a home directory or the process cwd. An unresolvable workspace
  writes nothing rather than guessing;
* under ``.robothor/``, so it is never mistaken for a deliverable — and not
  under ``.robothor/secret*``, which is the prefix ``secret_paths`` refuses;
* named for the run that wrote it, so the run can take its own files with it
  and the retention sweep can reap what a killed run orphaned.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SPILL_RETENTION_DAYS",
    "SPILL_DIRNAME",
    "STDERR_LIMIT",
    "STDOUT_LIMIT",
    "is_spill_readback",
    "prune_run_spills",
    "prune_spill_files",
    "shape_exec_result",
    "spill",
    "spill_paths_in",
    "truncate_stream",
]

#: How much of a command's output reaches the model. Unchanged from #583 on
#: purpose: what was wrong was not the size of the window but that everything
#: outside it ceased to exist.
STDOUT_LIMIT = 4_000
STDERR_LIMIT = 2_000

#: Where the whole stream goes. Relative to the workspace; see the header.
SPILL_DIRNAME = ".robothor/exec"

#: How long an orphaned spill is kept. These are working files — an agent pages
#: one back in the run that wrote it and never again — and the run reaps its own
#: on the way out, so this window only ever catches what a killed run left.
DEFAULT_SPILL_RETENTION_DAYS = 7

#: The two streams a command has. A closed set because the name reaches both a
#: filename and the result keys the model reads.
_STREAMS = ("stdout", "stderr")

#: Separator between the run stem and the rest of a spill filename. Two
#: underscores, and the stem below cannot contain one, so ``run-a__*`` can
#: never match a file belonging to run ``run-a-b``.
_SEP = "__"

#: The two path components a spill file sits in, split so the read-back check
#: can compare them one at a time rather than matching a substring.
_SPILL_ROOT_DIR, _SPILL_LEAF = SPILL_DIRNAME.split("/")

#: Exactly the filename :func:`spill` writes. A read-back is recognised by this
#: plus the directory plus the file EXISTING — see :func:`spill_paths_in`.
_SPILL_NAME = re.compile(r"^[A-Za-z0-9-]{1,120}__(?:stdout|stderr)__[0-9a-f]{12}\.txt$")

#: Everything from an unquoted `#` to the end of the line. Shell and Python
#: agree on the comment character, and a path named only inside a comment is
#: not a path the call is reading.
_COMMENT = re.compile(r"(?m)(?<![\"'])#[^\n]*$")

#: The tools through which an agent can actually read a file back.
_READBACK_TOOLS = frozenset({"read_file", "exec", "execute_code"})


def truncate_stream(text: str, limit: int, path: str = "") -> str:
    """One stream, cut visibly, and — when it was spilled — recoverably.

    Without *path* this is exactly #583's marker, which is what a failed spill
    degrades to: still honest about the cut, just with nothing better to offer
    than narrowing the command.
    """
    if len(text) <= limit:
        return text
    if path:
        tail = (
            f"[truncated: {limit} of {len(text)} chars shown — the full output is at "
            f"{path}; read_file it, or re-run with a narrower command]"
        )
    else:
        tail = (
            f"[truncated: {limit} of {len(text)} chars shown — re-run with a "
            "narrower command, or write the full output to a file and read_file it]"
        )
    return f"{text[:limit]}\n\n{tail}"


def _settings_workspace() -> Path | None:
    """The instance workspace, or None when there is no answer.

    Through ``settings.sources`` rather than the environment: the env-read
    ratchet is a one-way door, and ``ROBOTHOR_WORKSPACE`` is already declared
    there.
    """
    try:
        from robothor.settings.sources import workspace_path

        resolved = workspace_path()
        return resolved.resolve(strict=False) if resolved is not None else None
    except Exception:  # noqa: BLE001 - an unresolvable workspace refuses, never reads
        return None


def _spill_root(workspace: str | Path | None) -> Path | None:
    """``<workspace>/.robothor/exec``, or None when the workspace is unknown.

    The caller's workspace first, settings second — the idiom
    ``vision_batch._workspace_root`` uses for the same question, and for the
    same reason: an unresolvable workspace refuses rather than defaulting to
    somewhere on the operator's disk.
    """
    root = (
        Path(workspace).expanduser().resolve(strict=False) if workspace else _settings_workspace()
    )
    if root is None:
        return None
    return root / SPILL_DIRNAME


def _stem(run_id: str) -> str:
    """The run's part of a spill filename: alphanumerics and dashes, or "adhoc".

    The run id is minted by the engine, never by a model — but it reaches a
    path component, and a path component that has not been narrowed is a
    traversal waiting for the one caller who passes something else.
    """
    cleaned = "".join(c if (c.isalnum() or c == "-") else "-" for c in run_id).strip("-")
    return cleaned or "adhoc"


def spill(workspace: str | Path | None, text: str, *, stream: str, run_id: str = "") -> str:
    """The whole stream to a file the agent can page back, or "" if it cannot.

    Never raises. A disk that will not take the file degrades the result to
    #583's marker; turning a working command into a tool error because the
    spill failed would be a worse trade than the one this replaces.
    """
    if stream not in _STREAMS:
        return ""
    root = _spill_root(workspace)
    if root is None:
        return ""
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{_stem(run_id)}{_SEP}{stream}{_SEP}{uuid.uuid4().hex[:12]}.txt"
        # `errors="surrogateescape"` because `subprocess.run(text=True)` on a
        # command emitting raw bytes hands us lone surrogates, and a spill that
        # raised on those would fail exactly the diagnostics most worth keeping.
        path.write_text(text, encoding="utf-8", errors="surrogateescape")
        return str(path)
    except OSError as exc:
        logger.warning("exec could not spill %s: %s", stream, exc)
        return ""


def shape_exec_result(
    result: dict[str, Any],
    *,
    workspace: str | Path | None,
    run_id: str = "",
) -> dict[str, Any]:
    """One command's result as the model should read it.

    For each stream over its limit: a bounded head with a marker naming the
    path, plus ``<stream>_truncated``, ``<stream>_chars`` and ``<stream>_path``
    beside it. A stream that fit is returned untouched and carries no fields at
    all — a flag on output that lost nothing is a lie in the other direction,
    and it is the one that erodes trust in the flag.
    """
    if not isinstance(result, dict):
        return result
    shaped = dict(result)
    for stream, limit in (("stdout", STDOUT_LIMIT), ("stderr", STDERR_LIMIT)):
        text = shaped.get(stream)
        if not isinstance(text, str) or len(text) <= limit:
            continue
        path = spill(workspace, text, stream=stream, run_id=run_id)
        shaped[stream] = truncate_stream(text, limit, path)
        shaped[f"{stream}_truncated"] = True
        shaped[f"{stream}_chars"] = len(text)
        if path:
            shaped[f"{stream}_path"] = path
    return shaped


def spill_paths_in(tool_input: dict[str, Any] | None) -> list[str]:
    """Every argument token that RESOLVES to a spill file this engine wrote.

    Three conditions, and all three are needed:

    * the token is a real path token, taken after shell and Python comments are
      stripped — not a substring of one;
    * its name matches the filename this module writes
      (``<run>__<stream>__<hex>.txt``) and it sits directly in a
      ``.robothor/exec/`` directory;
    * **the file exists.**

    The first cut asked only whether ``.robothor/exec/`` and ``__`` appeared
    anywhere in the joined argument values, which made
    ``curl -s http://evil.invalid/api # .robothor/exec/a__b`` a read-back
    (hostile review I3). That string is free to write, so an agent — or an
    instruction injected into a page it fetched — could append it to every
    command and be exempt from the repeat-call guard and the no-progress loop
    detector for the rest of the run. Those two controls exist because a run
    once spent 333 requests and 704 seconds going nowhere.
    """
    found: list[str] = []
    for value in (tool_input or {}).values():
        if not isinstance(value, (str, int, float)):
            continue
        for token in _COMMENT.sub(" ", str(value)).split():
            candidate = token.strip("\"'`,;()[]{}").replace("\\", "/")
            if not candidate.endswith(".txt") or _SEP not in candidate:
                continue
            path = Path(candidate)
            if not _SPILL_NAME.match(path.name):
                continue
            if path.parent.name != _SPILL_LEAF or path.parent.parent.name != _SPILL_ROOT_DIR:
                continue
            try:
                if path.is_file():
                    found.append(candidate)
            except OSError:  # noqa: PERF203 - an unreadable path is simply not one
                continue
    return found


def is_spill_readback(tool_name: str, tool_input: dict[str, Any] | None) -> bool:
    """True when this call is the agent going to get the rest of its own output.

    It has to be exempt from the repeat-call guard and the no-progress
    detector. A guard that answers "you already read that" or "this command is
    not making progress" to an agent paging back the output the engine cut from
    it would be refusing the one remedy the marker told it to use.

    By resolved PATH rather than by a per-session ledger, because both controls
    that need the answer are handed a tool name and arguments and no session —
    and because a run restored from the database has an empty in-memory ledger
    while its spill files are still on disk.
    """
    if tool_name not in _READBACK_TOOLS:
        return False
    return bool(spill_paths_in(tool_input))


def _spill_files(workspace: str | Path | None, pattern: str) -> Iterator[Path]:
    root = _spill_root(workspace)
    if root is None or not root.is_dir():
        return iter(())
    return iter(sorted(root.glob(pattern)))


def _unlink(paths: Iterator[Path], older_than: float | None = None) -> int:
    removed = 0
    for path in paths:
        try:
            if older_than is not None and path.stat().st_mtime >= older_than:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the sweep
            logger.warning("the exec spill prune could not remove a file: %s", exc)
    return removed


def prune_run_spills(workspace: str | Path | None, run_id: str) -> int:
    """Delete the spills this run wrote. Called as the run ends.

    A run's spill exists so the agent can page its own output back while the
    run is alive; nothing reads it afterwards. Keyed on the run's own stem, so
    a concurrent run's files are never this run's business.
    """
    if not run_id:
        return 0
    return _unlink(_spill_files(workspace, f"{_stem(run_id)}{_SEP}*"))


def prune_spill_files(
    *,
    retention_days: int | None = None,
    workspace: str | Path | None = None,
    now: float | None = None,
) -> int:
    """Delete spills older than the window. The backstop, not the main path.

    A run killed between writing a spill and reaching its own cleanup never
    reaps anything, and without this that is a permanent leak in a directory
    no operator looks at — the defect ``vision_batch`` shipped for one release.
    ``retention_days <= 0`` disables the sweep rather than deleting everything:
    "keep for zero days" is far likelier to be a misconfiguration than an
    instruction.
    """
    days = DEFAULT_SPILL_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    return _unlink(_spill_files(workspace, "*.txt"), older_than=cutoff)
