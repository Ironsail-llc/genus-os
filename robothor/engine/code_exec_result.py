"""What the model reads back: bounded, marked, and never silently cut.

The last step of an ``execute_code`` call, and a different subject from the
three before it — whether the snippet may run (``code_exec_guards``), what it
runs as (``code_exec_process``), and what it may reach (``tool_proxy``). This
one has a single question: given what the process produced, what goes into the
context and what goes to disk.

The rule it exists for is the one ``exec`` used to get wrong. Truncation is
PAGINATION, not amputation: the cut says how much was cut, the whole output is
written where the agent can ``read_file`` it, and the result names the path. A
model cannot tell "the command printed forty lines" from "the command printed
four thousand and you are seeing the first eight percent", so it reasons from
the visible part as though it were the whole — measured on the four benchmark
tasks that scored worst, every one of them bulk extraction with its tail
silently gone.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from robothor.engine.act_observe import OTHER_METHOD, clean_url
from robothor.engine.code_exec_process import MAX_TIMEOUT_SECONDS
from robothor.engine.code_execution import MAX_STDERR_BYTES, SandboxResult, truncate_with_marker

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "HARD_CAP_MULTIPLIER",
    "WORKDIR_NAME",
    "recorded_http_calls",
    "recorded_spawns",
    "shape",
    "spill",
]

#: How much more than the model's stdout budget is kept for the spill file.
#: The spill exists so the agent can page the whole output back, so it has to
#: hold more than the inline cut — and it cannot hold everything, because
#: "everything" is whatever a runaway loop printed.
HARD_CAP_MULTIPLIER = 20

#: Where an oversized stdout is spilled. Under the workspace so the agent can
#: `read_file` it back; under `.robothor/` so it is never mistaken for a
#: deliverable; not under `.robothor/secret*`, which is the prefix
#: `secret_paths` refuses.
WORKDIR_NAME = ".robothor/execute_code"


def spill(workspace: Path, stdout: str) -> str:
    """Full stdout to a file the agent can page back, or "" if it cannot be written.

    The same trade the `analyze_image` batch makes: the model reads a bounded
    head and is told where the rest is, so truncation becomes pagination rather
    than the invisible amputation `exec` used to do at 4,000 characters.
    """
    try:
        root = workspace / WORKDIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"stdout-{uuid.uuid4().hex[:12]}.txt"
        path.write_text(stdout, encoding="utf-8")
        return str(path)
    except OSError as exc:
        logger.warning("execute_code could not spill stdout: %s", exc)
        return ""


#: The record file is refused unread past this. Generous for what the recorder
#: itself will write (200 exchanges x 2,000 body chars is under half of it) and
#: small enough that a same-uid snippet which replaced the file with a
#: gigabyte cannot park the event loop in `read_text`.
MAX_RECORD_BYTES = 1_048_576

#: How many distinct `(method, url, status)` lines the result lists. Past
#: this the most recent are kept and one marker says how many were elided.
MAX_HTTP_CALL_ENTRIES = 50


def _read_record(tools_dir: Path, name: str) -> list[Any] | None:
    """A recorder's file as a list, ``None`` when it was never written.

    Raises on a file that is too large or not a list; the handler turns that
    into ``"unreadable"``. The SIZE is checked before a byte of it is read:
    the file was written by a process the snippet controlled.
    """
    path = tools_dir / name
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > MAX_RECORD_BYTES:
        raise ValueError(f"{name} is {size} bytes; the cap is {MAX_RECORD_BYTES}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{name} is not a list")
    return raw


def recorded_http_calls(tools_dir: Path) -> list[dict[str, Any]] | None:
    """What the in-sandbox recorder saw the snippet do over HTTP, bounded again.

    ``None`` when there is no record at all — a recorder that failed to
    install writes nothing, and "absent" is a different answer from "saw
    nothing", so the result can say which. ``[]`` when it ran and the snippet
    made no request. Raises on a file that is too large or not the shape the
    recorder writes; the handler turns that into ``"unreadable"``. Re-bounded
    here rather than trusted.
    """
    from robothor.engine.sandbox_runtime.http_recorder import (
        MAX_RECORDED_BODY_CHARS,
        MAX_RECORDED_CALLS,
        RECORD_FILE,
    )

    raw = _read_record(tools_dir, RECORD_FILE)
    if raw is None:
        return None
    calls: list[dict[str, Any]] = []
    for item in raw[:MAX_RECORDED_CALLS]:
        if not isinstance(item, dict) or not item.get("method") or not item.get("url"):
            continue
        calls.append(
            {
                "method": _method(item["method"]),
                "url": clean_url(item["url"]),
                "status": int(item.get("status") or 0),
                "body": str(item.get("body") or "")[:MAX_RECORDED_BODY_CHARS],
                "truncated": bool(item.get("truncated")),
                "t": _number(item.get("t")),
            }
        )
    return calls


def recorded_spawns(tools_dir: Path) -> list[dict[str, Any]] | None:
    """What the in-sandbox spawn recorder saw the snippet start, bounded again.

    The same three answers as :func:`recorded_http_calls` — ``None`` (no
    record), ``[]`` (ran, nothing spawned), a list — and the same refusal of
    an oversized or malformed file. Every field is re-bounded: a process the
    snippet controlled wrote it.
    """
    from robothor.engine.sandbox_runtime.spawn_recorder import (
        MAX_ARGV_HEAD,
        MAX_RECORDED_BODY_CHARS,
        MAX_RECORDED_SPAWNS,
        RECORD_FILE,
    )

    raw = _read_record(tools_dir, RECORD_FILE)
    if raw is None:
        return None
    spawns: list[dict[str, Any]] = []
    for item in raw[: MAX_RECORDED_SPAWNS + 1]:
        if not isinstance(item, dict):
            continue
        if "dropped" in item and len(item) == 1:
            # The recorder's own marker: how many spawns fell past its cap.
            spawns.append({"dropped": max(0, _integer(item["dropped"]) or 0)})
            continue
        argv = item.get("argv_head")
        spawns.append(
            {
                "argv_head": [str(a)[:200] for a in (argv if isinstance(argv, list) else [])][
                    :MAX_ARGV_HEAD
                ],
                "program": _PROGRAM_CHARS.sub("", str(item.get("program") or ""))[:64],
                "method": _method(item.get("method")),
                "url": clean_url(item.get("url")),
                "returncode": _integer(item.get("returncode")),
                "status": _integer(item.get("status")),
                "body": str(item.get("body") or "")[:MAX_RECORDED_BODY_CHARS],
                "truncated": bool(item.get("truncated")),
                "shell": bool(item.get("shell")),
                "piped": bool(item.get("piped")),
                "to_file": bool(item.get("to_file")),
                "unclassified": bool(item.get("unclassified")),
                "t": _number(item.get("t")),
            }
        )
    return spawns[: MAX_RECORDED_SPAWNS + 1]


#: What a program's basename may be made of once it comes from the record.
_PROGRAM_CHARS = re.compile(r"[^A-Za-z0-9._+-]")


def _method(value: Any) -> str:
    """``""`` stays empty (not an HTTP spawn); a well-formed verb is kept
    upper-cased; anything else is ``OTHER_METHOD`` — neither read nor write,
    never quoted (hostile review I3: ``GET\n[SYSTEM] IGN``)."""
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text if _METHOD_SHAPE.match(text) else OTHER_METHOD


_METHOD_SHAPE = re.compile(r"^[A-Z]{3,10}$")


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _http_call_entries(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The requests as the model and the ledger read them: identical
    ``(method, url, status)`` collapsed into one line with a count, ordered by
    LAST occurrence. That order is load-bearing, not cosmetic: the ledger's
    "was this origin read after its last write" is answered by a read entry
    sitting after a write entry, with no index to carry. A spawned call also
    carries ``via``, ``returncode`` and, when its body said so, ``refused``,
    and collapses only with its own kind. Capped, with the most recent kept
    and one marker for the rest."""
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for call in calls:
        extra = {
            k: call[k]
            for k in ("via", "returncode", "refused", "outcome", "unobserved")
            if k in call
        }
        key = (call["method"], call["url"], call["status"], tuple(sorted(extra.items())))
        entry = grouped.pop(key, None) or {
            **{k: call[k] for k in ("method", "url", "status")},
            **extra,
        }
        entry["count"] = int(entry.get("count", 0)) + 1
        grouped[key] = entry  # re-inserted, so dict order is last-seen order
    entries = list(grouped.values())
    if len(entries) > MAX_HTTP_CALL_ENTRIES:
        elided = len(entries) - MAX_HTTP_CALL_ENTRIES
        entries = entries[-MAX_HTTP_CALL_ENTRIES:] + [{"elided": elided}]
    return entries


def shape(
    result: SandboxResult,
    *,
    server: Any,
    workspace: Path,
    stdout_cap: int,
    http_calls: list[dict[str, Any]] | None = None,
    http_recorder: str = "",
    spawns: list[dict[str, Any]] | None = None,
    spawn_recorder: str = "",
) -> dict[str, Any]:
    """The result the model reads: bounded, marked, and never silently cut.

    ``http_calls`` is the merged list of what both recorders saw over HTTP
    (``None`` when neither left a record); ``spawns`` is what the spawn
    recorder saw start. ``http_recorder`` and ``spawn_recorder`` are states to
    report about each record — ``"absent"`` or ``"unreadable"`` — so nobody
    reads "no ``http_calls``" as "no HTTP", or "no ``spawned``" as "no child".
    """
    full_stdout = result.stdout
    stdout, stdout_cut = truncate_with_marker(full_stdout, stdout_cap)
    stderr, stderr_cut = truncate_with_marker(result.stderr, MAX_STDERR_BYTES)

    shaped = SandboxResult(
        stdout=stdout,
        stderr=stderr,
        returncode=result.returncode,
        tool_call_count=server.calls_served,
        timed_out=result.timed_out,
        stdout_truncated=stdout_cut,
        stderr_truncated=stderr_cut,
    ).as_dict()

    if stdout_cut:
        path = spill(workspace, full_stdout)
        if path:
            shaped["stdout_file"] = path
            shaped["note"] = (
                f"Output was {len(full_stdout)} characters; the first {stdout_cap} are "
                f"above and the whole of it is at {path}. Work over that file "
                "rather than re-running the snippet to see the rest."
            )
    if result.timed_out:
        shaped["error"] = (
            "The snippet ran out of time. Its process group and every descendant "
            "the engine could still see were killed — but a child started with "
            "`start_new_session=True` whose parent then called `os._exit` is "
            "reached by neither, so do not background work you need stopped. Ask "
            f"for more time (`timeout`, up to {MAX_TIMEOUT_SECONDS}s), or do less."
        )
    if server.calls_served >= server.max_calls:
        shaped["tool_call_limit_reached"] = True
    if http_recorder:
        shaped["http_recorder"] = http_recorder
    if spawn_recorder:
        shaped["spawn_recorder"] = spawn_recorder
    if http_calls:
        # The requests, not the bodies: what the snippet did over the network
        # on its own, so the model and the observation ledger both see the
        # writes. The bodies stay out of the context — the point of the
        # unread-response count is that the snippet should have printed them.
        shaped["http_calls"] = _http_call_entries(http_calls)
    if spawns:
        from robothor.engine.code_exec_spawns import spawn_summary

        shaped.update(spawn_summary(spawns))
    return shaped
