#!/usr/bin/env python3
"""Report and prune write-only memory blocks. Dry-run by default.

2,092 of 2,492 blocks have never been read. They are written, stored, indexed
and paid for, and no agent has ever loaded one. Membership in the always-loaded
tier should be a curated working set, not an accumulating log.

Pruning is a soft delete: `pruned_at` is stamped and the row stays, so a block
that turns out to matter is one UPDATE away from returning. Every prune writes
a manifest row first — a soft delete with no record of which ids a run touched
is reversible in principle and irreversible in practice.

    python scripts/memory_block_prune.py                    # report only
    python scripts/memory_block_prune.py --apply --min-age-days 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.db.connection import get_connection  # noqa: E402
from robothor.memory.block_budget import live_tier_report  # noqa: E402

# Never prune these regardless of read count: they are the seeded core tier and
# a fresh instance legitimately has not read them yet.
PROTECTED_TYPES = ("system", "config", "persistent")


def find_candidates(min_age_days: int) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, tenant_id, block_name, block_type,
                   length(content) AS chars, write_count, last_written_at
            FROM agent_memory_blocks
            WHERE COALESCE(read_count, 0) = 0
              AND pruned_at IS NULL
              AND COALESCE(block_type, '') NOT IN %s
              AND last_written_at < NOW() - make_interval(days => %s)
            ORDER BY length(content) DESC
            """,
            (PROTECTED_TYPES, min_age_days),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def prune(candidates: list[dict], *, apply: bool) -> dict:
    if not candidates:
        return {"pruned": 0, "reclaimed_chars": 0}
    ids = [c["id"] for c in candidates]
    reclaimed = sum(c["chars"] or 0 for c in candidates)
    if not apply:
        return {"pruned": 0, "would_prune": len(ids), "reclaimed_chars": reclaimed}

    with get_connection() as conn:
        cur = conn.cursor()
        # Manifest first, in the same transaction: a prune whose record is
        # written afterwards has a window where the rows are gone and nothing
        # says which.
        for c in candidates:
            # Snapshot the full content, so a restore does not depend on the
            # soft-deleted row still being there.
            cur.execute(
                "INSERT INTO memory_block_prune_log "
                "(tenant_id, block_id, block_name, block_type, content_chars, "
                " write_count, reason, content_snapshot) "
                "SELECT %s, %s, %s, %s, %s, %s, 'never_read', content "
                "FROM agent_memory_blocks WHERE id = %s",
                (
                    c["tenant_id"],
                    c["id"],
                    c["block_name"],
                    c["block_type"],
                    c["chars"],
                    c["write_count"],
                    c["id"],
                ),
            )
        cur.execute(
            "UPDATE agent_memory_blocks SET pruned_at = NOW() WHERE id = ANY(%s)",
            (ids,),
        )
        pruned = cur.rowcount
        conn.commit()
    return {"pruned": pruned, "reclaimed_chars": reclaimed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    report = live_tier_report()
    print("TIER 1 CONTEXT BUDGET")
    print(
        f"  {report['total_blocks']} blocks, {report['total_chars']:,} chars, "
        f"~{report['total_tokens']:,} tokens (mode: {report['mode']})"
    )
    for tier, s in sorted(report["by_tier"].items(), key=lambda kv: -kv[1]["tokens"]):
        print(f"    {tier:<14} {s['blocks']:>5} blocks  ~{s['tokens']:>9,} tok")
    print(f"  over budget: {len(report['over_budget'])} blocks")
    for b in report["over_budget"][:10]:
        print(
            f"    {str(b['block_name'])[:44]:<44} {b['content_chars']:>8,} / "
            f"{b['max_chars']:,} (+{b['overflow_chars']:,})"
        )

    cands = find_candidates(args.min_age_days)
    print(f"\nWRITE-ONLY BLOCKS (never read, >{args.min_age_days}d old, unprotected)")
    print(f"  candidates: {len(cands)}, {sum(c['chars'] or 0 for c in cands):,} chars")
    for c in cands[:10]:
        print(
            f"    {str(c['block_name'])[:44]:<44} {c['chars']:>8,} chars  {c['write_count']} writes"
        )

    result = prune(cands, apply=args.apply)
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
