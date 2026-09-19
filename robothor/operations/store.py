"""PostgreSQL primitives for recoverable work and reviewable external effects.

All public operations require an explicit tenant. A transaction sets RLS as well
as filtering SQL. Leases are fenced: an expired worker cannot complete a newer
worker's claim. External effects are never retried on an uncertain outcome.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor

from robothor.db.connection import get_connection


class Conflict(ValueError):  # noqa: N818
    """A stale worker, approval, or conflicting idempotency key."""


class BudgetExceeded(ValueError):  # noqa: N818
    """A missing or exhausted spending allowance."""


def digest(payload: dict) -> str:
    """Canonical digest binds authorization to exact, JSON-serializable content."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class Operations:
    """One tenant's work ledger. Methods never infer a tenant from user input."""

    def __init__(self, tenant_id: str):
        if not tenant_id or not tenant_id.strip():
            raise ValueError("Explicit tenant required")
        self.tenant = tenant_id

    @contextmanager
    def transaction(self):
        """Yield a dict cursor under a transaction-local RLS identity."""
        with get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (self.tenant,))
                yield cur
            conn.commit()

    def audit(self, cur, entity: str, event: str, actor="system", detail=None):
        """Record transitions transactionally; avoid message bodies in audit details."""
        cur.execute(
            "INSERT INTO operation_audit(tenant_id,entity_id,event,actor,detail) "
            "VALUES(%s,%s,%s,%s,%s)",
            (self.tenant, str(entity), event, actor, Json(detail or {})),
        )

    def enqueue(
        self,
        kind: str,
        key: str,
        payload: dict,
        *,
        max_attempts=5,
        deadline_seconds=86400,
        cur=None,
    ) -> str:
        """Idempotently schedule a stage, optionally within the producer transaction."""
        if cur is None:
            with self.transaction() as cursor:
                return self.enqueue(
                    kind,
                    key,
                    payload,
                    max_attempts=max_attempts,
                    deadline_seconds=deadline_seconds,
                    cur=cursor,
                )
        if max_attempts < 1 or deadline_seconds < 1:
            raise ValueError("Positive attempts and deadline required")
        job_id = str(uuid4())
        cur.execute(
            "INSERT INTO operation_jobs(id,tenant_id,kind,dedup_key,payload,max_attempts,deadline) "
            "VALUES(%s,%s,%s,%s,%s,%s,now()+%s*interval '1 second') "
            "ON CONFLICT(tenant_id,kind,dedup_key) DO NOTHING RETURNING id",
            (job_id, self.tenant, kind, key, Json(payload), max_attempts, deadline_seconds),
        )
        row = cur.fetchone()
        if row:
            self.audit(cur, job_id, "work.enqueued")
            return str(row["id"])
        cur.execute(
            "SELECT id,payload FROM operation_jobs WHERE tenant_id=%s AND kind=%s AND dedup_key=%s",
            (self.tenant, kind, key),
        )
        row = cur.fetchone()
        if row["payload"] != payload:
            raise Conflict("Idempotency key already has a different payload")
        return str(row["id"])

    def claim(self, kind: str, *, lease_seconds=900) -> dict | None:
        """Claim one ready job with SKIP LOCKED; retire exhausted/expired jobs."""
        if lease_seconds < 1:
            raise ValueError("Positive lease required")
        with self.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET status='failed', error='deadline or attempts exhausted' "
                "WHERE tenant_id=%s AND kind=%s AND (status='pending' OR "
                "(status='running' AND lease_until<now())) AND (deadline<=now() OR attempts>=max_attempts)",
                (self.tenant, kind),
            )
            cur.execute(
                "SELECT id FROM operation_jobs WHERE tenant_id=%s AND kind=%s "
                "AND available_at<=now() AND deadline>now() AND attempts<max_attempts "
                "AND (status='pending' OR (status='running' AND lease_until<now())) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
                (self.tenant, kind),
            )
            row = cur.fetchone()
            if not row:
                return None
            token = str(uuid4())
            cur.execute(
                "UPDATE operation_jobs SET status='running',attempts=attempts+1,lease_token=%s,"
                "lease_until=now()+%s*interval '1 second',updated_at=now() WHERE id=%s AND tenant_id=%s RETURNING *",
                (token, lease_seconds, row["id"], self.tenant),
            )
            claimed = dict(cur.fetchone())
            self.audit(cur, str(row["id"]), "work.claimed")
            return claimed

    def complete(self, job_id: str, token: str, result: dict, *, cur=None):
        """Complete a current lease, atomically with domain writes when supplied a cursor."""
        if cur is None:
            with self.transaction() as cursor:
                return self.complete(job_id, token, result, cur=cursor)
        cur.execute(
            "UPDATE operation_jobs SET status='completed',result=%s,updated_at=now() "
            "WHERE tenant_id=%s AND id=%s AND lease_token=%s AND status='running' AND lease_until>now()",
            (Json(result), self.tenant, job_id, token),
        )
        if cur.rowcount != 1:
            raise Conflict("Work lease expired or replaced")
        self.audit(cur, job_id, "work.completed")
        return None

    def checkpoint(self, job_id: str, token: str, payload: dict):
        """Persist paid work before domain commit; a replacement may reuse it.

        The active lease fences writes. A checkpoint cannot change in place;
        completion replaces it with the final receipt in the domain transaction.
        """
        with self.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET result=%s,updated_at=now() "
                "WHERE tenant_id=%s AND id=%s AND lease_token=%s AND status='running' "
                "AND lease_until>clock_timestamp() AND (result IS NULL OR result=%s)",
                (Json(payload), self.tenant, job_id, token, Json(payload)),
            )
            if cur.rowcount != 1:
                raise Conflict("Work lease expired, replaced, or checkpoint changed")
            self.audit(cur, job_id, "work.checkpointed")

    def defer(self, job_id: str, token: str, reason: str, *, delay_seconds=60, busy=False):
        """Retry a known-safe stage; busy admission does not consume an attempt."""
        with self.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET status='pending',error=%s,lease_token=NULL,lease_until=NULL,"
                "attempts=attempts-%s,available_at=now()+%s*interval '1 second',updated_at=now() "
                "WHERE tenant_id=%s AND id=%s AND lease_token=%s AND status='running' AND lease_until>now()",
                (reason[:500], int(busy), max(1, delay_seconds), self.tenant, job_id, token),
            )
            if cur.rowcount != 1:
                raise Conflict("Work lease expired or replaced")

    def get_job(self, job_id):
        """Read a job only within this tenant."""
        with self.transaction() as cur:
            cur.execute(
                "SELECT * FROM operation_jobs WHERE tenant_id=%s AND id=%s", (self.tenant, job_id)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def set_budget(self, scope: str, limit: int, *, cur=None):
        """Set a finite allowance; reducing it never erases consumption."""
        if type(limit) is not int or limit < 0:
            raise ValueError("Budget must be nonnegative integer micro-USD")
        if cur is None:
            with self.transaction() as cursor:
                return self.set_budget(scope, limit, cur=cursor)
        cur.execute(
            "INSERT INTO operation_budgets(tenant_id,scope,limit_units) VALUES(%s,%s,%s) "
            "ON CONFLICT(tenant_id,scope) DO UPDATE SET limit_units=EXCLUDED.limit_units",
            (self.tenant, scope, limit),
        )
        self.audit(cur, scope, "budget.configured", detail={"limit_units": limit})
        return None

    def reserve(self, scope: str, key: str, amount: int) -> str:
        """Serialize reservations against a scope, including concurrent children."""
        return self.reserve_many([scope], key, amount)[0]

    def reserve_many(self, scopes, key: str, amount: int, *, active_only=False, cur=None):
        """Reserve every scope together, or none. Lock scopes in canonical order.

        A caller supplying a cursor must roll back the transaction on failure.
        ``active_only`` prevents a settled idempotency key from authorizing more
        work; ordinary replay can still retrieve its original reservation IDs.
        """
        if type(amount) is not int or amount < 0:
            raise ValueError("Reservation must be nonnegative integer micro-USD")
        scopes = sorted(set(scopes))
        if not scopes or any(not scope for scope in scopes):
            raise ValueError("At least one budget scope required")
        if cur is None:
            with self.transaction() as cursor:
                return self.reserve_many(scopes, key, amount, active_only=active_only, cur=cursor)
        reservations = []
        for scope in scopes:
            cur.execute(
                "SELECT * FROM operation_budgets WHERE tenant_id=%s AND scope=%s FOR UPDATE",
                (self.tenant, scope),
            )
            budget = cur.fetchone()
            if not budget:
                raise BudgetExceeded("No configured budget")
            cur.execute(
                "SELECT * FROM operation_reservations WHERE tenant_id=%s AND scope=%s AND dedup_key=%s",
                (self.tenant, scope, key),
            )
            existing = cur.fetchone()
            if existing:
                if existing["reserved_units"] != amount:
                    raise Conflict("Reservation key reused with a different amount")
                if active_only and existing["actual_units"] is not None:
                    raise Conflict("Settled reservation cannot authorize new work")
                reservations.append(str(existing["id"]))
                continue
            if budget["spent_units"] + budget["reserved_units"] + amount > budget["limit_units"]:
                raise BudgetExceeded("Spending allowance exhausted")
            reservation = str(uuid4())
            cur.execute(
                "INSERT INTO operation_reservations(id,tenant_id,scope,dedup_key,reserved_units) "
                "VALUES(%s,%s,%s,%s,%s)",
                (reservation, self.tenant, scope, key, amount),
            )
            cur.execute(
                "UPDATE operation_budgets SET reserved_units=reserved_units+%s WHERE tenant_id=%s AND scope=%s",
                (amount, self.tenant, scope),
            )
            reservations.append(reservation)
        return reservations

    def settle(self, reservation: str, actual: int):
        """Record consumption once, retaining overruns so future calls stop."""
        self.settle_many([reservation], actual)

    def settle_many(self, reservations, actual: int):
        """Settle a group atomically, preserving each scope on conflict or crash."""
        if type(actual) is not int or actual < 0:
            raise ValueError("Actual spend must be nonnegative integer micro-USD")
        reservations = sorted({str(r) for r in reservations})
        if not reservations:
            raise ValueError("At least one reservation required")
        with self.transaction() as cur:
            cur.execute(
                "SELECT * FROM operation_reservations WHERE tenant_id=%s AND id=ANY(%s::uuid[]) "
                "ORDER BY id FOR UPDATE",
                (self.tenant, reservations),
            )
            rows = cur.fetchall()
            if len(rows) != len(reservations):
                raise Conflict("Unknown reservation")
            if any(row["actual_units"] not in (None, actual) for row in rows):
                raise Conflict("Reservation already settled")
            # All settlement paths lock reservation rows before budget rows;
            # scope order also matches multi-scope admission and configuration.
            cur.execute(
                "SELECT scope FROM operation_budgets WHERE tenant_id=%s AND scope=ANY(%s) "
                "ORDER BY scope FOR UPDATE",
                (self.tenant, sorted({row["scope"] for row in rows})),
            )
            for row in rows:
                if row["actual_units"] is not None:
                    continue
                cur.execute(
                    "UPDATE operation_budgets SET reserved_units=reserved_units-%s,spent_units=spent_units+%s "
                    "WHERE tenant_id=%s AND scope=%s",
                    (row["reserved_units"], actual, self.tenant, row["scope"]),
                )
                cur.execute(
                    "UPDATE operation_reservations SET actual_units=%s WHERE tenant_id=%s AND id=%s",
                    (actual, self.tenant, row["id"]),
                )

    def receive(self, provider: str, event_id: str, payload: dict) -> bool:
        """Durably accept a provider event once; processing occurs separately."""
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO operation_inbox(tenant_id,provider,event_id,payload) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (self.tenant, provider, event_id, Json(payload)),
            )
            if cur.rowcount == 1:
                return True
            cur.execute(
                "SELECT payload FROM operation_inbox WHERE tenant_id=%s AND provider=%s AND event_id=%s",
                (self.tenant, provider, event_id),
            )
            if cur.fetchone()["payload"] != payload:
                raise Conflict("Provider event identity reused with different content")
            return False

    def propose(
        self, kind: str, key: str, payload: dict, *, expires_seconds=86400, cur=None
    ) -> str:
        """Create an immutable draft. Changed content needs a new key and approval."""
        if cur is None:
            with self.transaction() as cursor:
                return self.propose(kind, key, payload, expires_seconds=expires_seconds, cur=cursor)
        action = str(uuid4())
        cur.execute(
            "INSERT INTO operation_actions(id,tenant_id,kind,dedup_key,payload,payload_hash,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,now()+%s*interval '1 second') ON CONFLICT DO NOTHING RETURNING id",
            (action, self.tenant, kind, key, Json(payload), digest(payload), expires_seconds),
        )
        row = cur.fetchone()
        if row:
            self.audit(cur, action, "action.proposed")
            return action
        cur.execute(
            "SELECT id,payload_hash FROM operation_actions WHERE tenant_id=%s AND kind=%s AND dedup_key=%s",
            (self.tenant, kind, key),
        )
        row = cur.fetchone()
        if row["payload_hash"] != digest(payload):
            raise Conflict("Draft key already bound to different content")
        return str(row["id"])

    def decide(self, action: str, approved: bool, actor: str):
        """Persist a human decision. Caller must authenticate the actor before entry."""
        if not actor.startswith("operator:"):
            raise Conflict("Human operator decision required")
        with self.transaction() as cur:
            cur.execute(
                "UPDATE operation_actions SET status=%s,approved_hash=payload_hash,decided_by=%s,decided_at=now() "
                "WHERE tenant_id=%s AND id=%s AND status='review' AND expires_at>now()",
                ("approved" if approved else "rejected", actor, self.tenant, action),
            )
            if cur.rowcount != 1:
                raise Conflict("Draft is missing, expired, or already decided")
            self.audit(cur, action, "action.approved" if approved else "action.rejected", actor)

    def claim_action(self, *, kind=None, lease_seconds=120) -> dict | None:
        """Claim an approved exact action. Expired sends become UNKNOWN, never ready."""
        with self.transaction() as cur:
            cur.execute(
                "UPDATE operation_actions SET status='unknown' WHERE tenant_id=%s "
                "AND status='executing' AND lease_until<now()",
                (self.tenant,),
            )
            cur.execute(
                "SELECT * FROM operation_actions WHERE tenant_id=%s AND status='approved' "
                "AND expires_at>now() AND (%s IS NULL OR kind=%s) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
                (self.tenant, kind, kind),
            )
            row = cur.fetchone()
            if not row:
                return None
            if digest(row["payload"]) != row["approved_hash"]:
                raise Conflict("Approved payload changed")
            token = str(uuid4())
            cur.execute(
                "UPDATE operation_actions SET status='executing',lease_token=%s,"
                "lease_until=now()+%s*interval '1 second' WHERE tenant_id=%s AND id=%s",
                (token, lease_seconds, self.tenant, row["id"]),
            )
            row = dict(row)
            row["lease_token"] = token
            row["status"] = "executing"
            self.audit(cur, str(row["id"]), "action.claimed")
            return row

    def finish_action(self, action: str, token: str, status: str, receipt: dict):
        """Record evidence about a claimed effect without permitting blind retries."""
        if status not in {"completed", "unknown", "cancelled", "failed"}:
            raise ValueError("Invalid action outcome")
        if status == "completed" and not receipt.get("id"):
            raise Conflict("Provider acknowledgement ID required")
        with self.transaction() as cur:
            cur.execute(
                "UPDATE operation_actions SET status=%s,receipt=%s WHERE tenant_id=%s AND id=%s "
                "AND lease_token=%s AND status='executing' AND (%s != 'completed' OR lease_until>now())",
                (status, Json(receipt), self.tenant, action, token, status),
            )
            if cur.rowcount != 1:
                raise Conflict("Action is no longer owned by this executor")
            self.audit(cur, action, "action." + status)
