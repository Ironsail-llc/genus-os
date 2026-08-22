"""The CLI must register its runner, or spawning tools silently do not work.

`robothor/engine/tools/handlers/spawn.py` keeps the active runner in a module
global that `daemon.py` populates via ``set_runner`` at startup. Every tool
that spawns a sub-agent — including ``benchmark_run`` — reads it back through
``get_runner()``. The CLI built its own ``AgentRunner`` and never registered
it, so those tools failed with "Runner not available - benchmark_run requires
a running engine".

The practical cost was a 24-hour feedback loop: a benchmark suite could only
be exercised by the nightly cron inside the daemon, so a grader fix could not
be verified before it shipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

CLI_ENGINE = Path(__file__).resolve().parents[2] / "cli" / "engine.py"


def _runner_constructions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function body that constructs an AgentRunner."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "AgentRunner"
            ):
                found.append(node)
                break
    return found


def _calls_set_runner(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "set_runner"
        for n in ast.walk(fn)
    )


def test_every_cli_runner_is_registered_for_spawning() -> None:
    """Any function that builds an AgentRunner must also register it.

    Asserted structurally rather than by importing and running the CLI: the
    failure mode is a missing call, and a missing call is exactly what a
    mocked end-to-end test would paper over.
    """
    tree = ast.parse(CLI_ENGINE.read_text())
    builders = _runner_constructions(tree)
    assert builders, "expected robothor/cli/engine.py to construct an AgentRunner"

    unregistered = [fn.name for fn in builders if not _calls_set_runner(fn)]
    assert not unregistered, (
        "these CLI functions build an AgentRunner without calling set_runner(), "
        f"so sub-agent spawning and benchmarks silently fail in them: {unregistered}"
    )


def test_set_runner_makes_the_runner_retrievable() -> None:
    """Pin the contract the CLI depends on: set_runner -> get_runner."""
    from robothor.engine.tools import set_runner
    from robothor.engine.tools.handlers.spawn import get_runner

    sentinel = object()
    previous = get_runner()
    try:
        set_runner(sentinel, None)  # type: ignore[arg-type]
        assert get_runner() is sentinel
    finally:
        set_runner(previous, None)  # type: ignore[arg-type]
