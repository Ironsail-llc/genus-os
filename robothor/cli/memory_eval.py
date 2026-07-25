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
    run_suite,
)

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
    suite_path = Path(args.suite)
    if not suite_path.exists():
        print(f"Suite not found: {suite_path}")
        return 3

    tenant = args.tenant or EVAL_TENANT

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
    return exit_code_for(report, None)
