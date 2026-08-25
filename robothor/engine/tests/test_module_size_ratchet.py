"""God-object regrowth ratchet.

The 2026-08-24 architecture audit scored engine architecture 3/5 for one
dominant reason: runner.py was a 4,660-line god-object (execute ~1,100 lines,
_run_loop ~1,230) that every cross-cutting concern threads through, with
telegram.py (3,792) as the same disease in the delivery layer. Phase 1 of the
decomposition extracted the run-finalization cluster (~740 lines whose only
external dependency was self.config) into run_finalizer.py.

This ratchet makes the remaining sizes a one-way door: a change that grows a
capped module past its high-water mark fails CI with instructions, the same
drift-gate pattern as the guardrail-list and alert-name tests. When you
EXTRACT code and a module shrinks, lower its cap to the new size — that is
the point of a ratchet.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# module -> (cap_lines, rationale)
CAPS = {
    # Lowered 4000 -> 3200 after phase 2 (LLM-call + lifecycle clusters out).
    # Lowered 3200 -> 2900 after phase 4 (tool-call admission gates out).
    # 2900 -> 2950 held while the run-budget cluster left (deadline warning,
    # compaction trigger, runaway token caps -> run_budget.py).
    "robothor/engine/runner.py": 2950,
    # Lowered 3850 -> 3150 after the plan-mode cluster left (phase 3).
    # Lowered again after phase 3b (_setup_handlers closures -> methods).
    "robothor/engine/telegram.py": 2000,
    "robothor/engine/telegram_handlers.py": 1300,
    "robothor/engine/telegram_plan_mode.py": 900,
    "robothor/engine/run_finalizer.py": 1100,
    "robothor/engine/run_lifecycle.py": 800,
    "robothor/engine/run_llm_calls.py": 450,
    "robothor/engine/tool_admission.py": 400,
    "robothor/engine/run_budget.py": 88,
    "robothor/engine/chat.py": 1600,
    "robothor/engine/scheduler.py": 1600,
}


def test_capped_modules_do_not_regrow():
    over = []
    for rel, cap in CAPS.items():
        lines = len((REPO_ROOT / rel).read_text().splitlines())
        if lines > cap:
            over.append(f"{rel}: {lines} lines > cap {cap}")
    assert not over, (
        "module(s) grew past the decomposition ratchet — extract a cohesive "
        "cluster into its own module instead of adding to a god-object "
        f"(see run_finalizer.py's header for the pattern): {over}"
    )


def test_ratchet_caps_are_tight():
    """A cap far above the actual size is a dead gate. Keep each within 10%
    of reality so the ratchet actually ratchets — when you extract code,
    lower the cap."""
    loose = []
    for rel, cap in CAPS.items():
        lines = len((REPO_ROOT / rel).read_text().splitlines())
        if cap > lines * 1.10:
            loose.append(f"{rel}: cap {cap} vs actual {lines}")
    assert not loose, f"tighten these caps to (actual x 1.10) or less: {loose}"
