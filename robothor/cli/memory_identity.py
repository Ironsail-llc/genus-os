"""CLI: ``genus memory-identity sync|backfill|reverse``.

Thin wrappers over :mod:`robothor.memory.identity`. Every verb needs an
explicit ``--tenant``; none falls back to a default tenant. ``sync`` and
``backfill`` take ``--dry-run``, which writes nothing and reports what would
change. ``backfill`` is resumable: pass the ``last_fact_id`` it printed back as
``--resume-from``.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse


def _print(report: dict[str, Any], as_json: bool, summary: list[str]) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    for line in summary:
        print(line)


def cmd_memory_identity(args: argparse.Namespace) -> int:
    """Dispatch ``memory-identity <verb>``. Returns a process exit code."""
    if not getattr(args, "identity_command", None):
        print("usage: genus memory-identity {sync,backfill,reverse} --tenant T", file=sys.stderr)
        return 1

    from robothor.memory import identity

    verb = args.identity_command
    as_json = bool(getattr(args, "json_output", False))
    try:
        if verb == "sync":
            report = identity.sync_crm_entities(args.tenant, dry_run=args.dry_run)
            mode = "would change" if args.dry_run else "changed"
            _print(report, as_json, [
                f"tenant {args.tenant}: CRM identity sync ({mode})",
                f"  people:    {report['people']}",
                f"  companies: {report['companies']}",
            ])  # fmt: skip
            return 0
        if verb == "backfill":
            report = identity.backfill_mentions(
                args.tenant,
                dry_run=args.dry_run,
                batch_size=args.batch_size,
                resume_from=args.resume_from,
            )
            lines = [
                f"tenant {args.tenant}: mention backfill ({'dry run' if args.dry_run else 'written'})",
                f"  facts scanned: {report.get('facts_scanned', 0)}",
                f"  by method:     {report.get('by_method', {})}",
                f"  unresolved:    {len(report.get('unresolved', []))} distinct surface forms",
                f"  last fact id:  {report.get('last_fact_id')} (done: {report.get('done')})",
            ]
            if report.get("crm_unsynced"):
                lines.append(
                    f"  note: {report['crm_unsynced']} CRM record(s) have no node yet; "
                    "run `genus memory-identity sync` first"
                )
            _print(report, as_json, lines)
            return 0
        if verb == "reverse":
            facts = getattr(args, "fact", None)
            if bool(facts) == bool(getattr(args, "all_mentions", False)):
                print(
                    "error: name the facts to correct (--fact, repeatable) or pass --all to "
                    "reverse every mention the decision carries",
                    file=sys.stderr,
                )
                return 2
            report = identity.reverse_decision(
                args.tenant,
                args.decision,
                reason=args.reason,
                new_entity_id=args.entity,
                fact_ids=facts or None,
                dry_run=bool(getattr(args, "dry_run", False)),
            )
            _print(report, as_json, [
                ("would reverse" if report.get("dry_run") else "reversed")
                + f" decision {args.decision}; new decision: {report['new_decision_id']}",
                f"  mentions re-pointed: {len(report['mention_ids'])}",
                f"  facts to rebuild:     {report['fact_ids']}",
                f"  relations to rebuild: {report['relation_ids']}",
            ])  # fmt: skip
            return 0
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"unknown identity verb: {verb}", file=sys.stderr)
    return 1
