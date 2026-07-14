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
import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.feature_flags import Rip7Mode, rip_7_enforcement_mode

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class DriftDecision:
    """Outcome of a single pre-update drift check.

    * ``action`` — ``"proceed"`` for a clean write, ``"refuse"`` for a
      blocked one (only ever returned in enforce mode).
    * ``drift_detected`` — True iff the stored hash didn't match what
      the canonical formula produced over the stored row fields. False
      for clean rows and for any row when the mode is ``"off"``.
    * ``mode`` — the active enforcement mode at decision time, so the
      caller can log it alongside the action.
    """

    action: str  # "proceed" | "refuse"
    drift_detected: bool
    mode: Rip7Mode


def evaluate_drift(
    stored_hash: str | None,
    *,
    fact_text: str,
    tenant_id: str,
    category: str = "",
    person_id: str | None = None,
) -> DriftDecision:
    """Compare the stored content_hash against the canonical recompute.

    A ``None`` ``stored_hash`` is treated as "first touch since the
    migration backfill" — no drift, proceed silently. Otherwise the
    recomputed hash must match; on mismatch the decision is to
    ``"refuse"`` in ``"enforce"`` mode and ``"proceed"`` in
    ``"observe"`` / ``"alert"`` / ``"off"``. Either way the
    ``drift_detected`` flag tells the caller whether to audit-snapshot
    the row.
    """
    mode = rip_7_enforcement_mode()
    if mode == "off" or stored_hash is None:
        return DriftDecision(action="proceed", drift_detected=False, mode=mode)
    expected = compute_fact_hash(
        fact_text, tenant_id=tenant_id, category=category, person_id=person_id
    )
    if stored_hash == expected:
        return DriftDecision(action="proceed", drift_detected=False, mode=mode)
    if mode == "enforce":
        return DriftDecision(action="refuse", drift_detected=True, mode=mode)
    if mode == "alert":
        # The middle rung: allow the write, but put it in front of the operator.
        # Without this, promoting RIP 7 to "alert" would notify nobody.
        from robothor.engine.feature_flags import notify_guardrail_alert

        notify_guardrail_alert(
            guardrail_name="memory_drift",
            agent_id="memory",
            reason=(
                f"a memory_facts row changed underneath the engine (content-hash "
                f"drift, category={category!r}); enforce would refuse this write"
            ),
            tenant_id=tenant_id,
        )
    return DriftDecision(action="proceed", drift_detected=True, mode=mode)


def audit_snapshot(
    cur: Any,
    *,
    fact_id: int,
    tenant_id: str,
    fact_text: str | None,
    hash_at_snapshot: str | None,
    hash_expected: str | None,
    reason: str,
) -> int | None:
    """Append a row to memory_facts_audit and return its snapshot_id.

    Caller supplies a live psycopg cursor (so the insert stays in the
    same transaction as the calling writer). Returns ``None`` on insert
    error rather than raising — the audit row is best-effort and must
    never block the writer from emitting its own response.
    """
    try:
        cur.execute(
            """
            INSERT INTO memory_facts_audit
                (fact_id, tenant_id, fact_text,
                 content_hash_at_snapshot, content_hash_expected, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING snapshot_id
            """,
            (
                fact_id,
                tenant_id,
                fact_text,
                hash_at_snapshot,
                hash_expected,
                reason,
            ),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        logger.warning(
            "Failed to insert memory_facts_audit row for fact %d: %s",
            fact_id,
            exc,
        )
        return None
