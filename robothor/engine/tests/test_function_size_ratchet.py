"""The decomposition ratchet caps FILES, so the god-objects moved sideways.

`test_module_size_ratchet.py` has bounded module line counts for a while, and
it worked in the sense it was written for: no module regrew. But a file cap
rewards moving a cohesive cluster into a new module, and it says nothing about
the function that was the actual problem. Four extraction phases later, the two
functions the module ratchet's own docstring blames are still the two largest in
the engine, and `runner.py::execute` had GROWN.

Nothing in the repo bounds a function. `pyproject.toml` selects no complexity
rule — no C901, no PLR0915 — and `tools/schemas.py` is not in the module ratchet
at all, which is how it came to hold a single 3,520-line function: larger than
all of runner.py.

So this is the missing half, in the same shape as its sibling: existing
offenders are pinned at their current size and can only shrink, and anything
NEW over the threshold fails. It ratchets down as the decomposition work lands
rather than merely forbidding regrowth.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]

#: A function longer than this is a decomposition problem, not a style one.
MAX_NEW_FUNCTION_LINES = 200

#: Every function already over the line, pinned at its measured size
#: (2026-08-27). These may SHRINK — the test fails if one grows, and fails if
#: an entry is more than 10% larger than reality, so shrinking forces the cap
#: down with it. Delete an entry when its function drops under the threshold.
KNOWN_LARGE: dict[str, int] = {
    "tools/schemas.py::get_engine_schemas": 3520,
    "health.py::create_health_app": 1476,
    "runner.py::execute": 983,
    "runner.py::_run_loop": 772,
    "tools/handlers/gws.py::_handle_gws_tool": 453,
    "daemon.py::main": 411,
    "telegram.py::_run_interactive": 384,
    "tools/handlers/benchmark.py::_benchmark_run": 370,
    "analytics.py::get_agent_stats": 343,
    "daemon.py::_watchdog": 303,
    "chat.py::plan_approve": 286,
    "compaction.py::compact": 276,
    "llm_client.py::_call_llm": 269,
    "config.py::manifest_to_agent_config": 268,
    "scheduler.py::start": 266,
    "llm_client.py::_call_llm_streaming": 260,
    "telegram.py::run_agent": 257,
    "tools/handlers/experiment.py::_experiment_commit": 256,
    "telegram.py::_handle_goal_command": 242,
    "managed_agents/runner.py::run_on_managed_agents": 241,
    "runner.py::execute_deep": 224,
    "chat.py::run_approved": 218,
    "workflow.py::execute": 208,
}


def _measure() -> dict[str, int]:
    out: dict[str, int] = {}
    for py in sorted(_ENGINE.rglob("*.py")):
        if "/tests/" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text())
        except (OSError, SyntaxError):
            continue
        rel = py.relative_to(_ENGINE)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and getattr(
                n, "end_lineno", None
            ):
                out[f"{rel}::{n.name}"] = n.end_lineno - n.lineno
    return out


def test_no_new_oversized_function():
    """A function over the threshold must be decomposed, not merely moved."""
    measured = _measure()
    new = {
        k: v
        for k, v in measured.items()
        if v > MAX_NEW_FUNCTION_LINES and k not in KNOWN_LARGE
    }
    assert not new, (
        f"new function(s) over {MAX_NEW_FUNCTION_LINES} lines: {new}. Extract a "
        "cohesive step instead — moving it to another module satisfies the file "
        "ratchet and changes nothing about the function."
    )


def test_known_large_functions_do_not_grow():
    """The ones already over the line may only shrink."""
    measured = _measure()
    grew = {
        k: (cap, measured[k]) for k, cap in KNOWN_LARGE.items() if measured.get(k, 0) > cap
    }
    assert not grew, f"function(s) grew past their pinned size (cap, actual): {grew}"


def test_the_pins_track_reality():
    """A stale pin is a cap that stopped meaning anything. Shrinking a function
    must drag its cap down, or the ratchet quietly loosens."""
    measured = _measure()
    loose = {
        k: (cap, measured[k])
        for k, cap in KNOWN_LARGE.items()
        if k in measured and cap > measured[k] * 1.10
    }
    assert not loose, (
        f"pin(s) more than 10% above actual — tighten them (cap, actual): {loose}"
    )


def test_resolved_entries_are_removed():
    """An entry whose function is gone, or now under the threshold, is noise."""
    measured = _measure()
    stale = [
        k
        for k in KNOWN_LARGE
        if k not in measured or measured[k] <= MAX_NEW_FUNCTION_LINES
    ]
    assert not stale, f"remove from KNOWN_LARGE — no longer oversized: {stale}"
