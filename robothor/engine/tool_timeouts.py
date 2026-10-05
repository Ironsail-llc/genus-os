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

from typing import Any

from robothor.engine.tool_self_timed import SELF_TIMED_GRACE_SECONDS, self_timed_ceiling

__all__ = [
    "HARNESS_BUDGETED_TOOLS",
    "LONG_RUNNING_FLOOR_SECONDS",
    "LONG_RUNNING_TOOLS",
    "SELF_TIMED_GRACE_SECONDS",
    "budgeted_tool_name",
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
        # pr_review_prepare clones/fetches a whole repository (each git step is
        # bounded at 300 s by robothor.pr_review.checkout) before starting the
        # review job; pr_review_finalize posts a review, its thread replies and
        # resolutions, and the Chat announcement — a dozen API round-trips.
        # Cut short at 120 s, prepare left a half-fetched clone and finalize a
        # review half-posted (it resumes, but only on a retry).
        "pr_review_prepare",
        "pr_review_finalize",
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


#: The deferred-toolset meta-tool. It runs another tool, so it has no budget of
#: its own: the deadline wrapping it is the one the tool it runs would get.
_WRAPPER_TOOL = "tool_call"


def budgeted_tool_name(tool_name: str, arguments: Any = None) -> str:
    """The tool whose budget a call is spent on.

    For ``tool_call`` that is the tool it wraps. Observed 2026-10-05: a deferred
    agent's ``tool_call(name="claude_code_wait", arguments={"timeout_s": 1200})``
    was cut at the 120 s default, because the deadline was resolved for the
    wrapper's name. Anything that is not a plain tool name — missing, not a
    string, or another meta-tool the handler will refuse — earns no budget.
    """
    if tool_name != _WRAPPER_TOOL or not isinstance(arguments, dict):
        return tool_name
    inner = arguments.get("name")
    if not isinstance(inner, str) or not inner.strip() or inner.strip() == _WRAPPER_TOOL:
        return tool_name
    return inner.strip()


def resolve_tool_timeout(tool_name: str, configured: int, arguments: Any = None) -> int:
    """Per-tool wall-clock cap, in seconds. 0 means unlimited.

    One owner per budget: where the callee already bounds its own work, this
    layer must not impose a second, smaller bound. Pass the call's
    ``arguments`` so a ``tool_call`` is bounded as the tool it runs.
    """
    tool_name = budgeted_tool_name(tool_name, arguments)
    if tool_name in HARNESS_BUDGETED_TOOLS:
        return 0
    ceiling = self_timed_ceiling(tool_name)
    if ceiling:
        return max(configured, ceiling)
    if tool_name in LONG_RUNNING_TOOLS:
        return max(configured, LONG_RUNNING_FLOOR_SECONDS)
    return configured
