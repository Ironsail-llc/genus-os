"""External-drift detection for `memory_facts` (Rip 7).

Adapted from Hermes Agent's `_detect_external_drift` pattern
(`tools/memory_tool.py:516`). The Hermes version operates on
plain markdown files and writes `.bak.<ts>` siblings when the
on-disk content wouldn't round-trip its parser. We operate on a
Postgres table, so the SQL analog is:

* every row carries a `content_hash` SHA-256 over the user-
  visible columns (`fact_text`, `tenant_id`, `category`, `person_id`);
* every legitimate write through `store_fact()` / `update_fact()`
  refreshes the hash;
* `update_fact()` recomputes the expected hash from the stored
  row before applying its update, compares it to the persisted
  hash, and on mismatch snapshots the row into
  `memory_facts_audit` with `reason='pre_update_drift_detected'`
  and refuses the write (when the rip is enforcing) or merely
  logs (when the rip is in observe/alert mode).

Enforcement modes are controlled by the `ROBOTHOR_RIP_7_ENABLED`
feature flag (see `robothor.engine.feature_flags`). The
plan rolls this out as 7 days observe-only → 7 days alert-only →
enforce, with the operator inspecting the audit table at each
boundary.
"""

from __future__ import annotations

import hashlib


def compute_fact_hash(
    fact_text: str,
    *,
    tenant_id: str,
    category: str = "",
    person_id: str | None = None,
) -> str:
    """Return the canonical SHA-256 hex digest for a `memory_facts` row.

    Inputs are concatenated with a literal `'|'` delimiter so
    that swapping content between fields can't collide with a
    different row. The same formula is used in the migration's
    backfill (`069_memory_facts_drift.sql`); drift in either
    implementation will surface immediately because every existing
    row will start to mismatch on next update.
    """
    canonical = f"{fact_text or ''}|{tenant_id or ''}|{category or ''}|{person_id or ''}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
