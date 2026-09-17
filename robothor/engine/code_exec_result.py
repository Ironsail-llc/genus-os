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
import uuid
from typing import TYPE_CHECKING, Any

from robothor.engine.code_exec_process import MAX_TIMEOUT_SECONDS
from robothor.engine.code_execution import MAX_STDERR_BYTES, SandboxResult, truncate_with_marker

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["HARD_CAP_MULTIPLIER", "WORKDIR_NAME", "recorded_http_calls", "shape", "spill"]

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


def recorded_http_calls(tools_dir: Path) -> list[dict[str, Any]]:
    """What the in-sandbox recorder saw the snippet do over HTTP, bounded again.

    ``[]`` when the snippet made none, or when there is no record — a recorder
    that failed to install writes nothing, and nothing is the honest answer.
    Re-bounded here rather than trusted: the file was written by a process
    the snippet controlled.
    """
    from robothor.engine.sandbox_runtime.http_recorder import (
        MAX_RECORDED_BODY_CHARS,
        MAX_RECORDED_CALLS,
        RECORD_FILE,
    )

    path = tools_dir / RECORD_FILE
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    calls: list[dict[str, Any]] = []
    for item in raw[:MAX_RECORDED_CALLS] if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("method") or not item.get("url"):
            continue
        calls.append(
            {
                "method": str(item["method"])[:16].upper(),
                "url": str(item["url"])[:2048],
                "status": int(item.get("status") or 0),
                "body": str(item.get("body") or "")[:MAX_RECORDED_BODY_CHARS],
            }
        )
    return calls


def shape(
    result: SandboxResult,
    *,
    server: Any,
    workspace: Path,
    stdout_cap: int,
    http_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The result the model reads: bounded, marked, and never silently cut."""
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
    if http_calls:
        # The requests, not the bodies: what the snippet did over the network
        # on its own, so the model and the observation ledger both see the
        # writes. The bodies stay out of the context — the point of the
        # unread-response count is that the snippet should have printed them.
        shaped["http_calls"] = [
            {"method": c["method"], "url": c["url"], "status": c["status"]} for c in http_calls
        ]
    return shaped
