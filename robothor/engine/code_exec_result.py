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

import logging
import uuid
from typing import TYPE_CHECKING, Any

from robothor.engine.code_exec_process import MAX_TIMEOUT_SECONDS
from robothor.engine.code_execution import MAX_STDERR_BYTES, SandboxResult, truncate_with_marker

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["HARD_CAP_MULTIPLIER", "WORKDIR_NAME", "shape", "spill"]

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


def shape(
    result: SandboxResult, *, server: Any, workspace: Path, stdout_cap: int
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
            "the engine could still see were killed; a process that both left the "
            "group and detached itself may have survived. Ask for more with the "
            f"`timeout` parameter (up to {MAX_TIMEOUT_SECONDS}s), or do less per "
            "snippet."
        )
    if server.calls_served >= server.max_calls:
        shaped["tool_call_limit_reached"] = True
    return shaped
