"""How long one tool call may take, and who owns that bound.

``registry.execute`` wraps every handler in ``asyncio.timeout``. That deadline
is the LAST one to fire, or it is a bug: a handler cancelled mid-wait cannot
clean up what it was supervising, and for ``execute_code`` that meant a snippet
and everything it started left running on the host with no deadline at all.
So the rule this module encodes is one sentence — **where the callee already
bounds its own work, this layer must not impose a second, smaller bound** —
applied in three shapes:

* **harness-budgeted** tools cap each case against the suite's own
  ``timeout_seconds:`` and record an overrun as a timeout rather than a grade.
  A second cap out here can only cut a case short below the budget its suite
  declared, and the run is then filed against the agent. They get 0: unlimited.
* **long-running** tools are several sub-agent runs deep, so the agent-level
  default (120 s) is far too short. They get a 600 s floor.
* **self-timed** tools enforce their own wall clock AND advertise it to the
  model, so the registry's deadline has to sit ABOVE that advertised ceiling
  rather than under it. ``execute_code`` advertises 300 s by default and 900 s
  maximum; its effective cap was the agent's ``tool_timeout_seconds``, 120 s by
  default, so every snippet longer than two minutes was cancelled and orphaned.
  ``exec`` carried the identical 120-vs-900 mismatch and survived only because
  its child runs under ``subprocess.run(timeout=…)`` in a thread, which kills
  it regardless of what happens to the awaiting coroutine.

**Why a module rather than four copies.** It WAS four copies. ``runner.py``,
``run_llm_calls.py``, ``run_lifecycle.py`` and ``run_finalizer.py`` each
carried a verbatim ``_LONG_RUNNING_TOOLS`` / ``_HARNESS_BUDGETED_TOOLS`` /
``_resolve_tool_timeout``, copied rather than imported when the decomposition
phases moved code out of the runner — and three of them had already drifted:
``ask_user`` was added to the runner's set and to none of the others. Only the
runner's copy was live, so the drift had cost nothing yet, and the next reader
to call the nearest ``_resolve_tool_timeout`` would have paid for it. This is
the ``hardcoded-names-drift`` shape, in a table that decides whether a tool is
killed mid-work.
"""

from __future__ import annotations

__all__ = [
    "HARNESS_BUDGETED_TOOLS",
    "LONG_RUNNING_FLOOR_SECONDS",
    "LONG_RUNNING_TOOLS",
    "SELF_TIMED_GRACE_SECONDS",
    "resolve_tool_timeout",
    "self_timed_ceiling",
]

#: Tools whose work is several sub-agent runs, so the agent-level per-tool cap
#: (120s by default) is far too short. Kept at a 600s floor.
LONG_RUNNING_TOOLS = frozenset(
    {
        "benchmark_run",
        "benchmark_run_fleet",
        "benchmark_run_for_agent",
        "benchmark_compare",
        "experiment_measure",
        "spawn_agent",
        "spawn_agents",
        # Measured 2026-08-22 against 14 days of real calls: each of these died
        # at exactly the 120s default and NONE ever completed above it.
        # buddy_review_pass 8 of 10 (main had no buddy review since 08-19,
        # vision-monitor since 08-17), deep_reason 4 of 18, look 3 of 70.
        # detectors.find_tools_capped_at_timeout reports the next one.
        "buddy_review_pass",
        "deep_reason",
        "look",
        # Waits on a person; ask_user.bounded_timeout caps its own wait below
        # what this grants, so the two agree instead of racing.
        "ask_user",
    }
)

LONG_RUNNING_FLOOR_SECONDS = 600

#: Of those, the tools that already enforce their OWN per-task budget: the
#: benchmark harness caps each case at the suite's ``timeout_seconds:`` (or 900s
#: by default) and records an overrun as a timeout rather than a grade. A second,
#: smaller cap out here can only cut a case short *below* the budget its suite
#: declared, and the run is then filed against the agent.
#:
#: This list previously named ``benchmark_run`` only, while the tools the fleet
#: grader actually calls are ``benchmark_run_fleet`` and
#: ``benchmark_run_for_agent`` -- so the two tools that run every benchmark
#: inherited the 120s default. Measured 2026-08-22: agent-architect
#: ``fleet-analysis`` had never once completed above 120.0s across 91 completed
#: runs, against a 512s production mean with zero production timeouts.
HARNESS_BUDGETED_TOOLS = frozenset(
    {
        "benchmark_run",
        "benchmark_run_fleet",
        "benchmark_run_for_agent",
    }
)

#: Slack between a self-timed tool's OWN ceiling and the registry deadline that
#: wraps it. Big enough that the tool's kill path always fires first, small
#: enough that a tool which somehow never returns is still bounded.
SELF_TIMED_GRACE_SECONDS = 30

_self_timed_ceilings: dict[str, int] = {}


def self_timed_ceiling(tool_name: str) -> int:
    """The registry's bound for a tool that enforces its OWN wall clock, or 0.

    Read from the handlers' own constants rather than restated here, because a
    second copy of a ceiling is a second ceiling — and this module exists
    because four copies of the neighbouring table had already drifted. Cached:
    the values are module constants, and this sits on the path of every tool
    call.
    """
    if not _self_timed_ceilings:
        from robothor.engine.code_exec_process import DRAIN_GRACE_SECONDS
        from robothor.engine.tools.handlers.code_exec import MAX_TIMEOUT_SECONDS
        from robothor.engine.tools.handlers.filesystem import MAX_EXEC_TIMEOUT

        _self_timed_ceilings.update(
            {
                "exec": MAX_EXEC_TIMEOUT + SELF_TIMED_GRACE_SECONDS,
                "execute_code": (
                    MAX_TIMEOUT_SECONDS + int(DRAIN_GRACE_SECONDS) + SELF_TIMED_GRACE_SECONDS
                ),
            }
        )
    return _self_timed_ceilings.get(tool_name, 0)


def resolve_tool_timeout(tool_name: str, configured: int) -> int:
    """Per-tool wall-clock cap, in seconds. 0 means unlimited.

    One owner per budget: where the callee already bounds its own work, this
    layer must not impose a second, smaller bound.
    """
    if tool_name in HARNESS_BUDGETED_TOOLS:
        return 0
    ceiling = self_timed_ceiling(tool_name)
    if ceiling:
        return max(configured, ceiling)
    if tool_name in LONG_RUNNING_TOOLS:
        return max(configured, LONG_RUNNING_FLOOR_SECONDS)
    return configured
