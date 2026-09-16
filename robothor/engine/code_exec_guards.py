"""May this snippet run at all, and is the engine ready for one?

Four refusals and one hardening step, kept together and kept out of the
handler. They share a subject — everything that has to be true BEFORE a
subprocess exists — and none of them touches staging, spawning, or shaping a
result, which is what the rest of the handler is.

Each refusal is written to be read by a model: what is missing, and what to do
instead. "execute_code failed" teaches an agent nothing and it will try again.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["MAX_CODE_CHARS", "harden_this_process", "preflight"]

#: The most source one call may carry. A snippet is code; a caller sending a
#: megabyte of it is sending data, and data belongs in a file the code reads.
MAX_CODE_CHARS = 100_000


def preflight(code: str, proxy: Any) -> dict[str, Any] | None:
    """The refusal this call earns, or None to go ahead.

    Order is cheapest-first and, where it matters, safest-first: the snippet is
    checked for being a snippet at all, then for having a run behind it (which
    is where the allow-set comes from), then for the capability that makes this
    tool no wider than `exec`, and last for the sandbox — the one refusal that
    has to read live per-run state.
    """
    if not isinstance(code, str) or not code.strip():
        return {"error": "No code provided. Pass the Python source as `code`."}
    if len(code) > MAX_CODE_CHARS:
        return {
            "error": (
                f"Snippet is {len(code)} characters, over the {MAX_CODE_CHARS} limit. "
                "Write the data to a file and have the snippet read it."
            )
        }
    if proxy is None:
        return {
            "error": (
                "execute_code is only available inside an agent run — there is no "
                "tool proxy on this call."
            )
        }
    if "exec" not in getattr(proxy, "allowed", frozenset()):
        return {
            "error": (
                "execute_code needs the `exec` capability, and this agent's manifest "
                "does not grant it. Ask the operator to add `exec` to tools_allowed."
            )
        }
    return _refuse_if_sandboxed()


def _refuse_if_sandboxed() -> dict[str, Any] | None:
    """A containerised agent is refused, and told why.

    An agent declaring `sandbox: docker` runs its commands inside a container
    with `--network none` and no host environment. The tool proxy lives in the
    engine process, so a snippet in that container could not reach it — and
    running the snippet on the HOST instead would silently undo the isolation
    the manifest asked for. Refusing names the trade; falling back would hide
    it, which is the failure this codebase keeps recording.
    """
    from robothor.engine.sandbox import SandboxMode, get_current_sandbox

    sandbox = get_current_sandbox()
    if sandbox is None or sandbox.mode == SandboxMode.LOCAL:
        return None
    return {
        "error": (
            "execute_code is not available to a container-sandboxed agent: the "
            "tool proxy runs in the engine and the container has no route to it. "
            "Use `exec` for shell work, or call the tools directly from a turn."
        )
    }


def harden_this_process() -> None:
    """Make the ENGINE's own /proc entries root-only before a snippet exists.

    The daemon already does this at startup, and this is not a second copy of
    the control — it is the same function, called where the exposure is.
    Scrubbing the child's environment removes INHERITANCE, not the credentials
    from the engine, and the snippet's parent IS whatever process is running
    engine code: the daemon in production, and something else in a benchmark
    pod, a CLI or a test. ``/proc/<parent>/environ`` is readable by any
    same-uid process and a snippet is one — measured, one line returned all
    nine seeded credentials. ``harden_process`` is idempotent and never raises,
    so calling it here costs a ``prctl`` on a path that is about to fork an
    interpreter, and it means the guarantee holds for every entry point that
    can reach this tool rather than only for the one that remembered.

    A failure is logged, not fatal: a kernel without ``PR_SET_DUMPABLE`` is not
    a reason to refuse the tool, and the honest statement belongs in the log
    rather than in a result the model reads.

    A MITIGATION, not a boundary. The remedy is the engine's environment
    ceasing to hold application credentials at all —
    ``docs/runbooks/SOPS_BOOTSTRAP.md`` — after which procfs leaks only
    bootstrap values.
    """
    from robothor.engine.process_hardening import harden_process

    if not harden_process():
        logger.warning(
            "execute_code: could not set PR_SET_DUMPABLE=0 — this process's "
            "/proc entries stay readable to any same-uid process, including "
            "the snippet about to run"
        )
