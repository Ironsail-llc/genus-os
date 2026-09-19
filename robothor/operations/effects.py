"""External write ledger with conservative, operator-mediated reconciliation.

An HTTP timeout cannot prove that a remote write failed. Claim an effect in
the database before calling the provider; every noncompleted claim requires
reconciliation, including one abandoned by process death.
"""

from __future__ import annotations

import asyncio

from psycopg2.extras import Json

from robothor.operations.store import Conflict, Operations, digest


class UnresolvedEffect(Conflict):
    """The provider must be checked before repeating this external write."""


class Effects:
    def __init__(self, tenant_id):
        self.ops = Operations(tenant_id)

    def _begin(self, kind, key, payload):
        payload_hash = digest(payload)
        with self.ops.transaction() as cur:
            cur.execute(
                "INSERT INTO operation_effects(tenant_id,kind,dedup_key,payload_hash) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING dedup_key",
                (self.ops.tenant, kind, key, payload_hash),
            )
            if cur.fetchone():
                self.ops.audit(cur, key, "effect.started", detail={"kind": kind})
                return None
            cur.execute(
                "SELECT * FROM operation_effects WHERE tenant_id=%s AND kind=%s AND dedup_key=%s",
                (self.ops.tenant, kind, key),
            )
            row = cur.fetchone()
            if row["payload_hash"] != payload_hash:
                raise Conflict("External effect key reused with different content")
            if row["status"] != "completed":
                raise UnresolvedEffect("External effect requires provider reconciliation")
            return row["receipt"]

    def _finish(self, kind, key, status, receipt):
        with self.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_effects SET status=%s,receipt=%s,updated_at=now() "
                "WHERE tenant_id=%s AND kind=%s AND dedup_key=%s AND status='executing'",
                (status, Json(receipt), self.ops.tenant, kind, key),
            )
            if cur.rowcount != 1:
                raise Conflict("External effect state changed during execution")
            self.ops.audit(cur, key, "effect." + status, detail={"kind": kind})

    async def perform(self, kind, key, payload, call):
        existing = await asyncio.to_thread(self._begin, kind, key, payload)
        if existing is not None:
            return existing
        try:
            receipt = await call()
            if not isinstance(receipt, dict) or not receipt.get("id"):
                raise UnresolvedEffect("Provider acknowledgement ID missing")
        except asyncio.CancelledError:
            # The durable executing row is intentionally retained. No retry
            # interprets cancellation or process death as evidence of failure.
            raise
        except Exception:
            await asyncio.to_thread(self._finish, kind, key, "unknown", {})
            raise UnresolvedEffect("External effect requires provider reconciliation") from None
        await asyncio.to_thread(self._finish, kind, key, "completed", receipt)
        return receipt

    def reconcile(self, kind, key, receipt, actor, reason):
        """Record the provider receipt an authenticated human actually verified."""
        if not actor.startswith("operator:") or not reason.strip() or not receipt.get("id"):
            raise Conflict("Human reconciliation, reason and provider receipt required")
        with self.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_effects SET status='completed',receipt=%s,updated_at=now() "
                "WHERE tenant_id=%s AND kind=%s AND dedup_key=%s AND status IN ('unknown','executing') "
                "AND updated_at < now()-interval '2 minutes'",
                (Json(receipt), self.ops.tenant, kind, key),
            )
            if cur.rowcount != 1:
                # An explicit unknown result is no longer in-flight and may
                # be reconciled immediately; executing claims need a grace.
                cur.execute(
                    "UPDATE operation_effects SET status='completed',receipt=%s,updated_at=now() "
                    "WHERE tenant_id=%s AND kind=%s AND dedup_key=%s AND status='unknown'",
                    (Json(receipt), self.ops.tenant, kind, key),
                )
                if cur.rowcount != 1:
                    raise Conflict("Effect is missing, active or already reconciled")
            self.ops.audit(cur, key, "effect.reconciled", actor, {"kind": kind, "reason": reason})
