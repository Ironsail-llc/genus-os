"""CLI: ``genus memory-replay`` — replay real recall questions, two configurations.

A thin wrapper over :mod:`robothor.memory.replay`. ``--tenant`` is required and
must name an existing tenant; there is no default. The whole command runs
inside ``read_only_scope``: it writes nothing, and a write that slipped in would
fail. The report holds recalled memory, so its first line says so.

Exit codes: 0 replayed (also when there was nothing to replay), 2 refused (no
such tenant, a bad flag combination, an unknown preset).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse


def add_parser(subparsers: Any) -> None:
    """Register ``memory-replay`` on the top-level subparsers."""
    p = subparsers.add_parser(
        "memory-replay",
        help="Replay real recall questions through today's and the new recall (read-only)",
    )
    p.add_argument("--tenant", required=True, help="Tenant whose questions and memory to read")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-runs",
        action="store_true",
        help="The queries of past search_memory tool calls, read as the run could read",
    )
    source.add_argument(
        "--from-chat",
        action="store_true",
        help="Recent user chat turns, each read as its conversation's owner",
    )
    source.add_argument("--questions", default=None, help="File with one question per line")
    p.add_argument("--as-person", default=None, help="With --questions: read as this CRM person")
    p.add_argument("--as-user", default=None, help="With --questions: read as this user id")
    p.add_argument("--since", default=None, help="Only questions since: 7d, 12h, 30m or a date")
    p.add_argument("--limit", type=int, default=50, help="Questions to replay")
    p.add_argument("--sample", action="store_true", help="A seeded random sample, not the newest")
    p.add_argument("--seed", type=int, default=1, help="Seed for --sample")
    p.add_argument(
        "--compare",
        default=None,
        help="Two presets A,B (default today,new; also new-graph, new-segments)",
    )
    p.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    p.add_argument("--out", default=None, help="Write the report here instead of stdout")
    p.add_argument(
        "--redact",
        action="store_true",
        help="Replace recalled text and questions with ids and lengths",
    )


def _questions(
    args: argparse.Namespace, since: Any, skipped: list[dict[str, Any]]
) -> tuple[str, list[Any]]:
    from robothor.memory import replay

    pick = {"limit": args.limit, "sample": args.sample, "seed": args.seed}
    if args.from_runs:
        return "runs", replay.questions_from_runs(args.tenant, since=since, **pick)
    if args.from_chat:
        return "chat", replay.questions_from_chat(args.tenant, since=since, skipped=skipped, **pick)
    reader = replay.reader_for(args.tenant, person_id=args.as_person, user_id=args.as_user)
    return "file", replay.questions_from_file(args.questions, reader=reader, **pick)


def cmd_memory_replay(args: argparse.Namespace) -> int:
    from robothor.db.connection import read_only_scope
    from robothor.memory import replay

    if (args.as_person or args.as_user) and not args.questions:
        print("error: --as-person / --as-user apply to --questions only", file=sys.stderr)
        return 2
    try:
        compare = replay.resolve_compare(args.compare)
        since = replay.parse_since(args.since)
        replay.require_tenant(args.tenant)
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    skipped: list[dict[str, Any]] = []
    with read_only_scope():
        source, questions = _questions(args, since, skipped)
        report = asyncio.run(
            replay.run_replay(
                args.tenant,
                questions,
                compare=compare,
                redact=args.redact,
                source=source,
                skipped=skipped,
            )
        )

    rendered = (
        json.dumps(report, indent=2, default=str)
        if args.json_output
        else replay.format_text(report)
    )
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        print(replay.NOTICE)
        print("\n".join(replay.summary_lines(report)))
        print(f"report written to {args.out}")
    else:
        print(rendered)
    return 0
