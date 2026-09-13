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

#: Re-measured against the merged tree on 2026-09-13: four entries carried
#: 1-3 lines of inherited slack and were tightened to actual. The file's rule
#: is "pinned at their current size"; headroom nobody earned is where the
#: next function regrows.
#: Every function already over the line, pinned at its measured size
#: (2026-08-27). These may SHRINK — the test fails if one grows, and fails if
#: an entry is more than 10% larger than reality, so shrinking forces the cap
#: down with it. Delete an entry when its function drops under the threshold.
KNOWN_LARGE: dict[str, int] = {
    # 3520 -> 3454: the four human-in-the-loop schemas (ask_user plus the three
    # workflow-approval tools) left as one cluster, `_HUMAN_IN_THE_LOOP_SCHEMAS`.
    # ask_user's own schema is in that constant, so the new tool cost this
    # function nothing and paid down 66 lines on the way in.
    "tools/schemas.py::get_engine_schemas": 3453,
    # -29: every subsystem router mount extracted to _mount_subsystem_routers,
    # which is what made room for the /api/admin registration rather than
    # raising this number for it.
    # 1448 -> 1431: the engine-wide auth middleware moved out to
    # _install_engine_auth, which is what paid for threading the live scheduler
    # onto the signature (POST /api/admin/scheduler/reconcile has to act on
    # THIS process's job registry) instead of raising this number for it.
    # 1431 -> 1386: the /costs body moved to _cost_breakdown, which is what
    # paid for reporting an unreadable benchmark break-out as null rather than
    # as zero spend — a different claim about the fleet, and one this endpoint
    # was the last place to get wrong.
    "health.py::create_health_app": 1386,
    # 990 -> 989: the post-stall autoDream spawn moved to
    # run_lifecycle.spawn_post_stall_autodream ("recovery helper spawns" is
    # that module's own contract), which is what paid for classifying a
    # workflow-budget kill and letting it propagate.
    "runner.py::execute": 989,  # +7: task_id propagated onto the run at INSERT time
    "runner.py::_run_loop": 775,
    # +12: run/tenant threaded onto the signature, and the do-not-contact
    # refusal at the head of both outbound-mail branches. The check itself
    # lives in _dnc_refusal; only the two call sites are in here.
    # 465 -> 473: gws_calendar_create is the third outbound-mail branch —
    # Google invites every attendee by email — so it gets the same call site.
    # Five irreducible lines (normalise the list, call, branch, return) plus
    # three of comment; the guard itself is still in _dnc_refusal.
    "tools/handlers/gws.py::_handle_gws_tool": 473,
    # -14: fleet capacity init extracted to _init_fleet_capacity;
    # -26: structlog wiring extracted to _configure_structured_logging, which
    # is what made room for the startup provider-secrets load rather than
    # raising this number for it.
    # 371 -> 365: the Slack start block and the channel-registry warm-up both
    # moved into _start_channels, which is what made room for the warm-up
    # rather than raising this number for it.
    "daemon.py::main": 364,
    "telegram.py::_run_interactive": 384,
    # 370 -> 347: shaping the graded child (silent delivery, iteration cap,
    # deny-list, is_benchmark) moved to _shape_child_config, which is what paid
    # for resolving the child's execution tenant in here rather than raising
    # this number for it.
    # 347 -> 220: the whole per-task loop moved to _execute_suite_tasks, so the
    # suite-level concerns that now wrap it (resolve the execution tenant once,
    # hold the sandbox advisory lock for the suite) are visible in one place.
    # 220 -> 210: pinned at the measured size, not the size it happened to be
    # under. Ten lines of unearned headroom is where the next function regrows.
    "tools/handlers/benchmark.py::_benchmark_run": 210,
    # 343 -> 325: the benchmark break-out moved to _benchmark_spend, which is
    # what paid for un-scoping it from the production tenant (the graded
    # children now run as benchmark-sandbox) rather than raising this number.
    # 325 -> 326: +1 for reporting an unreadable break-out as None rather than
    # as zero spend, which is a different claim about the fleet.
    "analytics.py::get_agent_stats": 326,
    # 303 -> 301: the reconcile reporting moved to _log_reconcile, which is
    # what paid for reporting added/replaced as well as pruned.
    "daemon.py::_watchdog": 298,
    "chat.py::plan_approve": 286,
    "compaction.py::compact": 276,
    # -7: rate-limit wait and the malformed-tool-call verdict extracted;
    # +4: review-requested comments on the malformed-tool-call branch (why the
    # re-roll spends `attempt`, and what skipping `_handle_model_error` costs)
    # 267 -> 242: the per-model admission decision (spent credential, open
    # breaker, retired pool) went to _skip_model_reason and the per-call
    # timeout selection to _per_call_timeout. That is what paid for the
    # workflow-deadline clamp inside the attempt loop rather than raising
    # this number for it.
    # 242 -> 240: _skip_model_reason now returns the pool it looked up, so the
    # call site stopped needing its own _key_pool line — which is also what
    # restored the lazy lookup for models the walk skips.
    "llm_client.py::_call_llm": 240,
    "config.py::manifest_to_agent_config": 267,
    # scheduler.py::start is gone from this list: 266 -> 116. The three
    # hand-written registration blocks (heartbeat, worker, cron) became one
    # loop over schedule_reconcile.agent_job_specs, which is also what
    # reconcile consumes — the whole point of the extraction being that the
    # engine derives its wanted job set ONCE.
    # 261 -> 247: the per-delta tool_use event emission moved to
    # _emit_tool_call_events, which more than paid for accumulating the
    # streamed reasoning_details litellm's stream_chunk_builder drops and for
    # threading the rejected history into the replay digest.
    # 247 -> 245: its own per-call timeout selection now shares
    # _per_call_timeout with _call_llm, which more than paid for the same
    # workflow-deadline clamp landing on this chain walk too.
    "llm_client.py::_call_llm_streaming": 245,
    "telegram.py::run_agent": 257,
    "tools/handlers/experiment.py::_experiment_commit": 256,
    "telegram.py::_handle_goal_command": 242,
    "managed_agents/runner.py::run_on_managed_agents": 241,
    "runner.py::execute_deep": 224,
    "chat.py::run_approved": 218,
    # workflow.py::execute is gone from this list: 208 -> 179. The finalization
    # cluster (completion stamp, terminal status, the failed->timeout
    # reclassification) became _finalize_status, which is what paid for the
    # workflow-deadline scope and the orphan-step close-out rather than raising
    # this number for them.
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
    new = {k: v for k, v in measured.items() if v > MAX_NEW_FUNCTION_LINES and k not in KNOWN_LARGE}
    assert not new, (
        f"new function(s) over {MAX_NEW_FUNCTION_LINES} lines: {new}. Extract a "
        "cohesive step instead — moving it to another module satisfies the file "
        "ratchet and changes nothing about the function."
    )


def test_known_large_functions_do_not_grow():
    """The ones already over the line may only shrink."""
    measured = _measure()
    grew = {k: (cap, measured[k]) for k, cap in KNOWN_LARGE.items() if measured.get(k, 0) > cap}
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
    assert not loose, f"pin(s) more than 10% above actual — tighten them (cap, actual): {loose}"


def test_resolved_entries_are_removed():
    """An entry whose function is gone, or now under the threshold, is noise."""
    measured = _measure()
    stale = [k for k in KNOWN_LARGE if k not in measured or measured[k] <= MAX_NEW_FUNCTION_LINES]
    assert not stale, f"remove from KNOWN_LARGE — no longer oversized: {stale}"
