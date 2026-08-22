"""The per-tool timeout must never cut a benchmark case below its own budget.

`runner._run_loop` wraps every tool call in `asyncio.timeout(tool_timeout_seconds)`,
default 120s, and raises that floor for a hand-maintained set of long-running
tools. That set named ``benchmark_run`` -- but the two tools the fleet grader
actually calls are ``benchmark_run_fleet`` and ``benchmark_run_for_agent``, so
the tools that run *every* benchmark inherited the 120s default while a tool
nothing schedules got the raised floor.

The benchmark harness already owns a per-task budget (`_resolve_task_timeout`:
900s by default, or the suite's own ``timeout_seconds:``). An outer per-tool cap
can only truncate a case *below* the budget its suite declared, which files a
healthy agent's heaviest case as a timeout. Measured 2026-08-22: agent-architect
`fleet-analysis` has never once completed above 120.0s across 91 completed runs,
while the same agent's production mean is 512s with zero production timeouts.

The rule this encodes: one owner per budget. Where the harness owns it, the
runner must not impose a second, smaller one -- and the set of such tools is
derived from what is actually registered, so it cannot drift out of sync again.
"""

from __future__ import annotations

import re
from pathlib import Path

from robothor.engine.runner import _HARNESS_BUDGETED_TOOLS, _LONG_RUNNING_TOOLS

_TOOL_NAME = re.compile(r"""["']name["']\s*:\s*["']([a-z][a-z0-9_]{2,})["']""")


def _registered_tool_names() -> set[str]:
    root = Path(__file__).resolve().parents[2]
    names: set[str] = set()
    for rel in ("engine/tools/schemas.py", "api/mcp.py"):
        path = root / rel
        if path.exists():
            names |= set(_TOOL_NAME.findall(path.read_text()))
    return names


def test_every_registered_benchmark_runner_tool_is_harness_budgeted() -> None:
    """No registered ``benchmark_run*`` tool may inherit the 120s default."""
    registered = _registered_tool_names()
    assert "benchmark_run_for_agent" in registered, "tool discovery is broken"

    runners = {n for n in registered if n.startswith("benchmark_run")}
    missing = sorted(runners - _HARNESS_BUDGETED_TOOLS)
    assert not missing, (
        "these registered benchmark tools would be capped at the 120s default, "
        "truncating cases below the budget their suite declared: " + ", ".join(missing)
    )


def test_harness_budgeted_tools_are_uncapped() -> None:
    """A harness-budgeted tool gets timeout 0 — the harness is the sole owner."""
    from robothor.engine.runner import _resolve_tool_timeout

    for name in sorted(_HARNESS_BUDGETED_TOOLS):
        assert _resolve_tool_timeout(name, 120) == 0, f"{name} must be uncapped"


def test_other_long_running_tools_keep_a_floor() -> None:
    """Sub-agent spawns are still bounded — only harness-owned work is uncapped."""
    from robothor.engine.runner import _resolve_tool_timeout

    assert _resolve_tool_timeout("spawn_agent", 120) == 600
    assert _resolve_tool_timeout("spawn_agent", 900) == 900
    assert "spawn_agent" in _LONG_RUNNING_TOOLS


def test_ordinary_tools_are_unaffected() -> None:
    """An ordinary tool keeps the agent's configured cap."""
    from robothor.engine.runner import _resolve_tool_timeout

    assert _resolve_tool_timeout("search_memory", 120) == 120
    assert _resolve_tool_timeout("search_memory", 0) == 0
