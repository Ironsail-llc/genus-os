#!/usr/bin/env python3
"""Read-only: what would change if decay consumed the repaired inputs?

Calls the real compute_decay_score with live values and again with shadow
values. Reimplementing the formula in SQL would test a different function than
production runs — the exact mistake this codebase keeps finding.

Writes nothing.
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from robothor.db.connection import get_connection  # noqa: E402
from robothor.memory.lifecycle import compute_decay_score  # noqa: E402

PROTECTED = ("decision", "preference", "resolution")


def main() -> int:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, category, importance_score, outcome_failures, "
            "       last_accessed, access_count, reinforcement_count, "
            "       COALESCE(last_accessed_shadow, created_at), access_count_shadow "
            "FROM memory_facts WHERE is_active = TRUE"
        )
        rows = cur.fetchall()

    before_retire, after_retire = set(), set()
    lifted = 0
    for (fid, cat, imp, fails, last_acc, acc, reinf, last_acc_s, acc_s) in rows:
        imp = float(imp or 0.0)
        d_before = compute_decay_score(last_acc, acc or 0, reinf or 0, imp, fails or 0)
        d_after = compute_decay_score(last_acc_s, acc_s or 0, reinf or 0, imp, fails or 0)
        if d_after > d_before:
            lifted += 1
        protected = (cat or "") in PROTECTED
        # The live sweep predicate (lifecycle.prune_low_quality_facts).
        if not protected and d_before < 0.1 and imp < 0.3 and (acc or 0) == 0:
            before_retire.add(fid)
        if not protected and d_after < 0.1 and imp < 0.3 and (acc_s or 0) == 0:
            after_retire.add(fid)

    print(f"active facts scored      : {len(rows)}")
    print(f"decay score raised       : {lifted}")
    print(f"would retire BEFORE      : {len(before_retire)}")
    print(f"would retire AFTER       : {len(after_retire)}")
    print(f"newly protected by fix   : {len(before_retire - after_retire)}")
    newly_at_risk = after_retire - before_retire
    print(f"newly AT RISK            : {len(newly_at_risk)}")
    if newly_at_risk:
        print("  ^ the backfill is supposed to be monotonically safer;")
        print(f"    investigate before promoting: {sorted(newly_at_risk)[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
