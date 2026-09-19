"""Revisioned business evidence, reviewed attribution and current retention.

Only trusted provider workers ingest records. Humans bind exact observed identities;
models neither merge businesses nor declare orders fulfilled.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.business_models import (
    BusinessPage,
    BusinessScan,
    ObservationKey,
    OrderRecord,
    PracticeRecord,
    SignupRecord,
)
from robothor.sales.service import operator


def retention_metrics(placed, dates, now=None):
    now = now or datetime.now(UTC)
    dates = sorted(dates)
    result = {
        "first_order_placed": placed.astimezone(UTC).isoformat() if placed else None,
        "completed_orders": len(dates),
        "first_completed_order": dates[0].astimezone(UTC).isoformat() if dates else None,
        "requires_review": False,
    }
    for key, start, end in [
        ("repeat_within_30_days", 0, 30),
        ("active_days_31_60", 30, 60),
        ("active_days_61_90", 60, 90),
    ]:
        mature = bool(dates) and now >= dates[0] + timedelta(days=end)
        result[key] = (
            any(timedelta(days=start) < d - dates[0] <= timedelta(days=end) for d in dates[1:])
            if mature
            else None
        )
    return result


class BusinessObservations:
    def __init__(self, sales):
        self.sales = sales
        self.ops = sales.ops
        self.tenant = sales.tenant

    def _lock(self, cur, source, account):
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (digest([self.tenant, "business-observations", source, account]),),
        )

    @staticmethod
    def _identity(row):
        return digest(
            [
                row["source"],
                row["account_id"],
                row["external_id"],
                row["data"]["business_unit_id"],
                row["data"]["name"],
            ]
        )

    def observe(
        self, source, account_id, kind, external_id, revision, observed_at, data, *, cur=None
    ):
        key = ObservationKey(
            source=source,
            account_id=account_id,
            kind=kind,
            external_id=external_id,
            revision=revision,
            observed_at=observed_at,
        )
        record = (
            {"practice": PracticeRecord, "signup": SignupRecord, "order": OrderRecord}[key.kind]
            .model_validate(data)
            .model_dump(mode="json")
        )
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.observe(
                    source,
                    account_id,
                    kind,
                    external_id,
                    revision,
                    key.observed_at,
                    record,
                    cur=cursor,
                )
        self._lock(cur, source, account_id)
        cur.execute(
            "SELECT * FROM sales_business_observations WHERE tenant_id=%s AND source=%s AND account_id=%s AND kind=%s AND external_id=%s FOR UPDATE",
            (self.tenant, source, account_id, kind, external_id),
        )
        old = cur.fetchone()
        fingerprint = digest(record)
        if old:
            if key.observed_at < old["observed_at"]:
                raise Conflict("An older observation cannot replace current evidence")
            cur.execute(
                "SELECT data_hash FROM sales_business_observation_history WHERE tenant_id=%s AND observation_id=%s AND revision=%s LIMIT 1",
                (self.tenant, old["id"], revision),
            )
            known = cur.fetchone()
            if known and known["data_hash"] != fingerprint:
                raise Conflict("Source revision reused with different evidence")
            if key.observed_at == old["observed_at"] and (
                revision != old["revision"] or fingerprint != old["data_hash"]
            ):
                raise Conflict("One observation time has conflicting evidence")
            if kind == "order" and record["practice_id"] != old["data"]["practice_id"]:
                raise Conflict("Order reassignment requires explicit reconciliation")
            if revision == old["revision"] and fingerprint == old["data_hash"]:
                cur.execute(
                    "UPDATE sales_business_observations SET observed_at=%s WHERE tenant_id=%s AND id=%s",
                    (key.observed_at, self.tenant, old["id"]),
                )
                return str(old["id"])
        observation_id = str(old["id"]) if old else str(uuid4())
        version = old["version"] + 1 if old else 1
        cur.execute(
            "INSERT INTO sales_business_observations(id,tenant_id,source,account_id,kind,external_id,revision,version,observed_at,data,data_hash) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,source,account_id,kind,external_id) "
            "DO UPDATE SET revision=EXCLUDED.revision,version=EXCLUDED.version,observed_at=EXCLUDED.observed_at,data=EXCLUDED.data,data_hash=EXCLUDED.data_hash",
            (
                observation_id,
                self.tenant,
                source,
                account_id,
                kind,
                external_id,
                revision,
                version,
                key.observed_at,
                Json(record),
                fingerprint,
            ),
        )
        cur.execute(
            "INSERT INTO sales_business_observation_history(tenant_id,observation_id,version,revision,observed_at,data,data_hash) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                self.tenant,
                observation_id,
                version,
                revision,
                key.observed_at,
                Json(record),
                fingerprint,
            ),
        )
        self.ops.audit(
            cur, observation_id, "business.observed", detail={"kind": kind, "version": version}
        )
        practice_id = external_id if kind == "practice" else record.get("practice_id")
        practice_ids = {
            x for x in [practice_id, (old["data"].get("practice_id") if old else None)] if x
        }
        if practice_ids:
            cur.execute(
                "SELECT b.*,o.source,o.account_id,o.external_id,o.data FROM sales_customer_bindings b "
                "JOIN sales_business_observations o ON o.tenant_id=b.tenant_id AND o.id=b.observation_id "
                "WHERE b.tenant_id=%s AND o.source=%s AND o.account_id=%s AND o.external_id=ANY(%s)",
                (self.tenant, source, account_id, list(practice_ids)),
            )
            bindings = list(cur.fetchall())
            for binding in bindings:
                if self._identity(binding) != binding["identity_hash"]:
                    cur.execute(
                        "UPDATE sales_customer_bindings SET status='held' WHERE tenant_id=%s AND observation_id=%s",
                        (self.tenant, binding["observation_id"]),
                    )
                    self.sales.escalate(
                        binding["prospect_id"],
                        "Business identity changed; customer association requires review",
                        cur=cur,
                    )
                self._changed(cur, binding["prospect_id"], f"{observation_id}:{version}")
        return observation_id

    def _changed(self, cur, prospect_id, event):
        self.sales.require(prospect_id, cur)
        cur.execute(
            "UPDATE sales_prospects SET outcome_version=outcome_version+1,updated_at=now() WHERE tenant_id=%s AND id=%s",
            (self.tenant, prospect_id),
        )
        cur.execute(
            "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND status IN ('review','approved') AND payload->>'prospect_id'=%s",
            (self.tenant, str(prospect_id)),
        )
        self.ops.enqueue(
            "sales.activation",
            f"business:{prospect_id}:{event}",
            {"prospect_id": str(prospect_id), "milestone": "business_observation_changed"},
            cur=cur,
        )
        self.ops.audit(
            cur, str(prospect_id), "business.customer_state_changed", detail={"observation": event}
        )

    def bind(self, prospect_id, observation_id, expected_revision, actor, reason):
        operator(actor)
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
            raise ValueError("Business association review reason required")
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT * FROM sales_business_observations WHERE tenant_id=%s AND id=%s AND kind='practice'",
                (self.tenant, observation_id),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Observed practice missing")
            self._lock(cur, row["source"], row["account_id"])
            cur.execute(
                "SELECT * FROM sales_business_observations WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (self.tenant, observation_id),
            )
            row = cur.fetchone()
            p = self.sales.require(prospect_id, cur)
            if p["external_company_id"]:
                raise Conflict("Legacy customer attribution needs an explicit migration")
            if row["revision"] != expected_revision:
                raise Conflict("Practice revision changed; review current evidence")
            cur.execute(
                "SELECT * FROM sales_customer_bindings WHERE tenant_id=%s AND observation_id=%s",
                (self.tenant, observation_id),
            )
            existing = cur.fetchone()
            if existing and str(existing["prospect_id"]) != str(prospect_id):
                raise Conflict("Practice already linked to another customer")
            cur.execute(
                "SELECT o.source,o.account_id FROM sales_customer_bindings b JOIN sales_business_observations o "
                "ON o.tenant_id=b.tenant_id AND o.id=b.observation_id WHERE b.tenant_id=%s AND b.prospect_id=%s",
                (self.tenant, prospect_id),
            )
            if any(
                r["source"] != row["source"] or r["account_id"] != row["account_id"]
                for r in cur.fetchall()
            ):
                raise Conflict("One authoritative business account per customer is required")
            identity = self._identity(row)
            if (
                existing
                and existing["status"] == "confirmed"
                and existing["identity_hash"] == identity
            ):
                return
            cur.execute(
                "INSERT INTO sales_customer_bindings(tenant_id,observation_id,prospect_id,reviewed_revision,identity_hash,status,reviewed_by,reason) "
                "VALUES(%s,%s,%s,%s,%s,'confirmed',%s,%s) ON CONFLICT(tenant_id,observation_id) DO UPDATE SET reviewed_revision=EXCLUDED.reviewed_revision,"
                "identity_hash=EXCLUDED.identity_hash,status='confirmed',reviewed_by=EXCLUDED.reviewed_by,reason=EXCLUDED.reason,reviewed_at=now()",
                (
                    self.tenant,
                    observation_id,
                    prospect_id,
                    expected_revision,
                    identity,
                    actor,
                    reason.strip(),
                ),
            )
            self.ops.audit(
                cur,
                str(prospect_id),
                "business.customer_bound",
                actor,
                {
                    "observation_id": str(observation_id),
                    "revision": expected_revision,
                    "reason": reason.strip(),
                },
            )
            self._changed(cur, prospect_id, "binding:" + str(uuid4()))

    def commit_page(self, job, data):
        """Atomically persist a validated read page, state updates and its next cursor."""
        page = BusinessPage.model_validate(data)
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT payload FROM operation_jobs WHERE tenant_id=%s AND id=%s AND kind='sales.business' "
                "AND status='running' AND lease_token=%s AND lease_until>clock_timestamp() FOR UPDATE",
                (self.tenant, job["id"], job["lease_token"]),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Business read lease expired or replaced")
            scan = BusinessScan.model_validate(row["payload"])
            if any(
                getattr(page, key) != getattr(scan, key)
                for key in ("source", "account_id", "kind", "practice_id", "after")
            ):
                raise Conflict("Business page identity changed during the read")
            if page.next_cursor is not None and (
                not page.items
                or page.next_cursor == scan.after
                or page.next_cursor in scan.seen_cursors
                or len(scan.seen_cursors) >= 999
            ):
                raise Conflict("Business page cursor requires reconciliation")
            for item in page.items:
                if page.kind == "order" and item.data.get("practice_id") != scan.practice_id:
                    raise Conflict("Order page practice identity changed")
                self.observe(
                    page.source,
                    page.account_id,
                    page.kind,
                    item.external_id,
                    item.revision,
                    page.observed_at,
                    item.data,
                    cur=cur,
                )
            if page.next_cursor:
                following = scan.model_dump(mode="json")
                following.update(
                    after=page.next_cursor, seen_cursors=[*scan.seen_cursors, page.next_cursor]
                )
                self.ops.enqueue("sales.business", digest(following), following, cur=cur)
            self.ops.complete(
                job["id"],
                job["lease_token"],
                {
                    "records": len(page.items),
                    "next_cursor": page.next_cursor,
                    "observed_at": page.observed_at.isoformat(),
                    "scan_id": scan.scan_id,
                    "final": page.next_cursor is None,
                },
                cur=cur,
            )

    def records(self, *, kind="practice", source=None, account_id=None, after=None):
        if kind not in {"practice", "signup", "order"}:
            raise ValueError("Business resource required")
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT o.*,b.prospect_id,b.status AS binding_status,b.reviewed_revision,b.reason AS review_reason "
                "FROM sales_business_observations o LEFT JOIN sales_customer_bindings b "
                "ON b.tenant_id=o.tenant_id AND b.observation_id=o.id WHERE o.tenant_id=%s AND o.kind=%s "
                "AND (%s::text IS NULL OR o.source=%s) AND (%s::text IS NULL OR o.account_id=%s) "
                "AND (%s::uuid IS NULL OR o.id>%s::uuid) ORDER BY o.id LIMIT 101",
                (self.tenant, kind, source, source, account_id, account_id, after, after),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return {
            "items": rows[:100],
            "next_cursor": str(rows[99]["id"]) if len(rows) > 100 else None,
        }

    def retention(self, prospect_id, now=None):
        with self.ops.transaction() as cur:
            self.sales.require(prospect_id, cur)
            cur.execute(
                "SELECT b.status,o.source,o.account_id,o.external_id,o.data AS practice_data FROM sales_customer_bindings b JOIN sales_business_observations o "
                "ON o.tenant_id=b.tenant_id AND o.id=b.observation_id WHERE b.tenant_id=%s AND b.prospect_id=%s",
                (self.tenant, prospect_id),
            )
            bindings = list(cur.fetchall())
            if not bindings:
                return None
            if any(b["status"] != "confirmed" for b in bindings):
                return {
                    **{
                        key: (True if key == "requires_review" else None)
                        for key in retention_metrics(None, [])
                    },
                    "coverage_complete": False,
                    "account_ready": None,
                    "signed_up_at": None,
                }
            dates, placed, signups, ready = [], [], [], False
            for b in bindings:
                cur.execute(
                    "SELECT kind,data FROM sales_business_observations WHERE tenant_id=%s AND source=%s AND account_id=%s AND kind IN ('signup','order') AND data->>'practice_id'=%s",
                    (self.tenant, b["source"], b["account_id"], b["external_id"]),
                )
                for item in cur.fetchall():
                    data = item["data"]
                    if item["kind"] == "order":
                        if data["placed_at"]:
                            placed.append(datetime.fromisoformat(data["placed_at"]))
                        if data["fulfillment"] == "verified":
                            dates.append(datetime.fromisoformat(data["fulfilled_at"]))
                    else:
                        signups.append(datetime.fromisoformat(data["signed_up_at"]))
                        ready = ready or (
                            data["account_ready"]
                            and b["practice_data"]["active"]
                            and data["business_unit_id"] == b["practice_data"]["business_unit_id"]
                        )
            result = retention_metrics(min(placed) if placed else None, dates, now)
            # Current pages prove individual observations, never a complete cohort.
            result["coverage_complete"] = False
            for window in ("repeat_within_30_days", "active_days_31_60", "active_days_61_90"):
                result[window] = None
            return {
                **result,
                "signed_up_at": min(signups).isoformat() if signups else None,
                "account_ready": ready if signups else None,
            }
