"""CLI: ``robothor memory-eval`` — run the memory retrieval benchmark.

Seeds fixture facts into an isolated tenant, runs the real retrieval path,
scores recall/temporal/verbatim/persona, prints a report, and cleans up.
Requires a live PostgreSQL + local Ollama (embeddings + reranker).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.memory.eval import (
    EVAL_TENANT,
    EvalPreconditionError,
    exit_code_for,
    format_report,
    preflight,
    record_benchmark_row,
    report_to_benchmark_row,
    require_eval_tenant,
    run_suite,
)
from robothor.memory.eval_answer import format_answers_markdown, run_answers
from robothor.memory.eval_extraction import format_extraction_markdown, run_extraction_eval
from robothor.memory.eval_world_seed import WORLD_TENANT, format_world_report, run_world

if TYPE_CHECKING:
    import argparse

_DEFAULT_SUITE = "docs/benchmarks/memory/suite.yaml"

#: Where --record-baseline writes the world answer-arm baseline.
BASELINE_DIR = Path("docs/benchmarks/memory")


def cmd_memory_eval(args: argparse.Namespace) -> int:
    """Run the memory benchmark.

    Exit codes are three-way on purpose: 0 passed, 2 the suite ran and cases
    failed, 3 the suite could not run. Collapsing 3 into 2 is what let this
    eval sit broken behind row-level security while still looking like an
    ordinary failing test.
    """
    if getattr(args, "extraction", False):
        if getattr(args, "world", False) or getattr(args, "answers", False):
            print("memory-eval cannot run: --extraction runs on its own, not with --world")
            return 3
        return _cmd_extraction(args)
    if getattr(args, "answers", False):
        if not getattr(args, "world", False):
            print("memory-eval cannot run: --answers needs --world")
            return 3
        return _cmd_answers(args)
    if getattr(args, "world", False):
        return _cmd_world(args)

    suite_path = Path(args.suite)
    if not suite_path.exists():
        print(f"Suite not found: {suite_path}")
        return 3

    tenant = args.tenant or EVAL_TENANT
    # Before preflight, which already opens a connection: the suite deletes
    # every fact in its tenant, so a real tenant must never get that far.
    try:
        require_eval_tenant(tenant)
    except EvalPreconditionError as e:
        print(f"memory-eval cannot run: {e}")
        return 3

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

    Exit 0 when it ran, 3 when it could not (or scored nothing): there is no
    world floor yet. Runs in its own tenant (``memory-eval-world`` by default),
    never the nightly suite's ``memory-eval``. Not recorded to benchmark_results:
    the fleet reads the latest ``memory`` row without a suite filter, so a
    world score would silently replace the suite's. Baselines for the world
    are their own later step.
    """
    tenant = args.tenant or WORLD_TENANT
    if tenant == EVAL_TENANT:
        print(
            f"memory-eval cannot run: --world may not use {EVAL_TENANT!r}, the nightly suite's "
            f"tenant; use {WORLD_TENANT!r} (the default)"
        )
        return 3
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
    # No floor exists for the world yet, so the suite's would make every run
    # exit 2 (raw_detail alone cannot pass through search_facts). 0 = it ran,
    # 3 = nothing was scored.
    return 0 if int(report.get("total") or 0) > 0 else 3


def _cmd_answers(args: argparse.Namespace) -> int:
    """Answer-and-judge comparison of several systems on the world.

    Writes the JSON report to ``--report`` (with a Markdown summary beside it)
    or to stdout. ``--record-baseline`` also writes both under
    ``docs/benchmarks/memory/``. Never touches benchmark_results: the suite
    owns that row.
    """
    import json

    from robothor.memory.eval_systems import make_systems

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    try:
        make_systems(systems)
    except ValueError as e:
        print(f"memory-eval cannot run: {e}")
        return 3

    tenant = args.tenant or WORLD_TENANT
    if tenant == EVAL_TENANT:
        print(
            f"memory-eval cannot run: --world may not use {EVAL_TENANT!r}, the nightly suite's "
            f"tenant; use {WORLD_TENANT!r} (the default)"
        )
        return 3
    blocked = preflight(tenant)
    if blocked:
        print(f"memory-eval cannot run: {blocked}")
        return exit_code_for(None, blocked)

    try:
        report = asyncio.run(
            run_answers(
                args.world_seed,
                args.world_size,
                args.split,
                systems=systems,
                reference=args.reference,
                token_budget=args.token_budget,
                repeats=args.repeats,
                answer_model=args.answer_model,
                judge_model=args.judge_model,
                limit_per_stratum=args.limit_per_stratum,
                tenant_id=tenant,
                concurrency=args.concurrency,
                cache_dir=args.cache_dir,
                cleanup=not args.keep,
                progress_path=args.progress,
            )
        )
    except (EvalPreconditionError, ValueError) as e:
        print(f"memory-eval cannot run: {e}")
        return 3

    payload = json.dumps(report, indent=2, default=str)
    markdown = format_answers_markdown(report)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        path.with_suffix(".md").write_text(markdown, encoding="utf-8")
        print(markdown)
        print(f"report: {path} (summary: {path.with_suffix('.md')})")
    else:
        print(payload)
        print("\n<!-- markdown -->\n")
        print(markdown)
    if args.record_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        summary = {k: v for k, v in report.items() if k != "records"}
        (BASELINE_DIR / "world-baseline.json").write_text(
            json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (BASELINE_DIR / "world-baseline.md").write_text(markdown, encoding="utf-8")
        print(f"recorded baseline under {BASELINE_DIR}")
    return 0


def _write_report(report: dict[str, Any], markdown: str, report_path: str | None) -> None:
    """The JSON report and its Markdown summary to ``report_path``, or stdout."""
    import json

    payload = json.dumps(report, indent=2, default=str)
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        path.with_suffix(".md").write_text(markdown, encoding="utf-8")
        print(markdown)
        print(f"report: {path} (summary: {path.with_suffix('.md')})")
    else:
        print(payload)
        print("\n<!-- markdown -->\n")
        print(markdown)


def _cmd_extraction(args: argparse.Namespace) -> int:
    """Measure conversation fact extraction on the labelled set.

    No database and no tenant: the real extraction is a model call and a
    parse, scored against hand labels by the validated judge. Exit 0 when it
    ran, 3 when it could not (labels invalid, the judge failed validation, or a
    named model that remote generation would ignore). Never recorded to
    benchmark_results.
    """
    fixtures = Path(args.fixtures)
    if not fixtures.exists():
        print(f"Fixtures not found: {fixtures}")
        return 3
    try:
        report = asyncio.run(
            run_extraction_eval(
                fixtures,
                model=args.extraction_model,
                judge_model=args.judge_model,
                repeats=args.repeats,
                concurrency=args.concurrency,
            )
        )
    except (EvalPreconditionError, ValueError) as e:
        print(f"memory-eval cannot run: {e}")
        return 3
    _write_report(report, format_extraction_markdown(report), args.report)
    return 0
