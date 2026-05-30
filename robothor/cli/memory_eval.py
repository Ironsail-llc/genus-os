"""CLI: ``robothor memory-eval`` — run the memory retrieval benchmark.

Seeds fixture facts into an isolated tenant, runs the real retrieval path,
scores recall/temporal/verbatim/persona, prints a report, and cleans up.
Requires a live PostgreSQL + local Ollama (embeddings + reranker).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.memory.eval import EVAL_TENANT, format_report, run_suite

if TYPE_CHECKING:
    import argparse

_DEFAULT_SUITE = "docs/benchmarks/memory/suite.yaml"


def cmd_memory_eval(args: argparse.Namespace) -> int:
    suite_path = Path(args.suite)
    if not suite_path.exists():
        print(f"Suite not found: {suite_path}")
        return 1

    report = asyncio.run(
        run_suite(
            suite_path,
            tenant_id=args.tenant or EVAL_TENANT,
            cleanup=not args.keep,
        )
    )
    print(format_report(report, as_json=args.json_output))
    # Non-zero exit when any case failed, so CI can gate on it.
    return 0 if report["passed"] == report["total"] else 2
