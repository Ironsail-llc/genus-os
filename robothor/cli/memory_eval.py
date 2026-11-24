"""CLI: ``robothor memory-eval`` — run the memory retrieval benchmark.

Seeds fixture facts into an isolated tenant, runs the real retrieval path,
scores recall/temporal/verbatim/persona, prints a report, and cleans up.
Requires a live PostgreSQL + local Ollama (embeddings + reranker).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.memory.eval import (
    EVAL_TENANT,
    EvalPreconditionError,
    exit_code_for,
    format_report,
    preflight,
    record_benchmark_row,
    report_to_benchmark_row,
    run_suite,
)
from robothor.memory.eval_world_seed import format_world_report, run_world

if TYPE_CHECKING:
    import argparse

_DEFAULT_SUITE = "docs/benchmarks/memory/suite.yaml"


def cmd_memory_eval(args: argparse.Namespace) -> int:
    """Run the memory benchmark.

    Exit codes are three-way on purpose: 0 passed, 2 the suite ran and cases
    failed, 3 the suite could not run. Collapsing 3 into 2 is what let this
    eval sit broken behind row-level security while still looking like an
    ordinary failing test.
    """
    if getattr(args, "world", False):
        return _cmd_world(args)

    suite_path = Path(args.suite)
    if not suite_path.exists():
        print(f"Suite not found: {suite_path}")
        return 3

    tenant = args.tenant or EVAL_TENANT

    # NB: robothor.cli's main() already calls load_instance_env(), which reads
    # /etc/robothor/robothor.env AND the engine's systemd drop-in and fills in
    # anything the caller did not set. So this process already carries the
    # production flag posture; do not re-implement that here.

    blocked = preflight(tenant)
    if blocked:
        print(f"memory-eval cannot run: {blocked}")
        return exit_code_for(None, blocked)

    try:
        report = asyncio.run(run_suite(suite_path, tenant_id=tenant, cleanup=not args.keep))
    except EvalPreconditionError as e:
        print(f"memory-eval cannot run: {e}")
        return exit_code_for(None, str(e))

    print(format_report(report, as_json=args.json_output))

    if getattr(args, "record", False):
        row = report_to_benchmark_row(
            report,
            suite_path=str(suite_path),
            triggered_by=getattr(args, "triggered_by", "manual"),
        )
        if record_benchmark_row(row):
            print(
                f"recorded: benchmark_results agent_id=memory "
                f"pass_rate={row['pass_rate']} ({row['passed']}/{row['total_cases']})"
            )
        else:
            # Deliberately not fatal: a reporting failure must not turn a
            # passing eval into a failing process. Persistent absence is caught
            # by the fleet's staleness check, not by this exit code.
            print("warning: could not record the result to benchmark_results")

    return exit_code_for(report, None)


def _cmd_world(args: argparse.Namespace) -> int:
    """Seed the synthetic world, score its cases through search_facts, clean up.

    Same three-way exit codes as the suite. Not recorded to benchmark_results:
    the fleet reads the latest ``memory`` row without a suite filter, so a
    world score would silently replace the suite's. Baselines for the world
    are their own later step.
    """
    tenant = args.tenant or EVAL_TENANT
    blocked = preflight(tenant)
    if blocked:
        print(f"memory-eval cannot run: {blocked}")
        return exit_code_for(None, blocked)

    try:
        report = asyncio.run(
            run_world(
                args.world_seed,
                args.world_size,
                args.split,
                tenant_id=tenant,
                cleanup=not args.keep,
            )
        )
    except EvalPreconditionError as e:
        print(f"memory-eval cannot run: {e}")
        return exit_code_for(None, str(e))

    print(format_world_report(report, as_json=args.json_output))
    if getattr(args, "record", False):
        print("note: --record is ignored with --world (world results are not recorded yet)")
    return exit_code_for(report, None)
