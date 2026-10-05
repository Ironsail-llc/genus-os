"""The registry bound for tools that enforce their OWN wall clock.

Split out of :mod:`robothor.engine.tool_timeouts`, which owns the rule ("where
the callee already bounds its own work, this layer must not impose a second,
smaller bound") and re-exports these names. This module holds the one shape of
that rule that needs the handlers' constants: a **self-timed** tool enforces
its own ceiling AND advertises it to the model, so the registry's deadline has
to sit ABOVE that advertised ceiling rather than under it (``execute_code``
advertised 900 s while the registry cancelled it at 120 s; ``claude_code_wait``
waits up to 1800 s on a coding job).
"""

from __future__ import annotations

__all__ = ["SELF_TIMED_GRACE_SECONDS", "self_timed_ceiling"]

#: Slack between a self-timed tool's OWN ceiling and the registry deadline that
#: wraps it. Big enough that the tool's kill path always fires first, small
#: enough that a tool which somehow never returns is still bounded.
SELF_TIMED_GRACE_SECONDS = 30

_self_timed_ceilings: dict[str, int] = {}


def self_timed_ceiling(tool_name: str) -> int:
    """The registry's bound for a tool that enforces its OWN wall clock, or 0.

    Read from the handlers' own constants rather than restated here, because a
    second copy of a ceiling is a second ceiling. Cached: the values are module
    constants, and this sits on the path of every tool call.
    """
    if not _self_timed_ceilings:
        from robothor.engine.code_exec_process import (
            DRAIN_GRACE_SECONDS,
            MAX_TIMEOUT_SECONDS,
        )
        from robothor.engine.tools.handlers.claude_code import _MAX_WAIT
        from robothor.engine.tools.handlers.filesystem import MAX_EXEC_TIMEOUT

        _self_timed_ceilings.update(
            {
                # Waits on a coding job up to its own clamped timeout_s (and never
                # past the run's deadline: run_pacing.clamp_tool_timeout).
                "claude_code_wait": _MAX_WAIT + SELF_TIMED_GRACE_SECONDS,
                "exec": MAX_EXEC_TIMEOUT + SELF_TIMED_GRACE_SECONDS,
                "execute_code": (
                    MAX_TIMEOUT_SECONDS + int(DRAIN_GRACE_SECONDS) + SELF_TIMED_GRACE_SECONDS
                ),
            }
        )
    return _self_timed_ceilings.get(tool_name, 0)
