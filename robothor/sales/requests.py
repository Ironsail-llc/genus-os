"""Durable bounded research briefs for the native sales fleet."""

from datetime import UTC, datetime
from math import ceil
from typing import Literal
from uuid import uuid4

from psycopg2.extras import Json
from pydantic import Field, field_validator

from robothor.operations.store import Conflict
from robothor.sales.models import Contract, SalesSettings
from robothor.sales.service import operator


class ResearchRequest(Contract):
    request_key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=10, max_length=4000)
    buying_case: str = Field(min_length=1, max_length=80)
    target_companies: int = Field(ge=1, le=1000, strict=True)

    @field_validator("request_key", "title", "query", "buying_case")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Nonblank request fields required")
        return value.strip()


class RequestChange(Contract):
    status: Literal["active", "paused", "cancelled"]
    expected_revision: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=10, max_length=2000)


class Requests:
    def __init__(self, sales):
        self.sales = sales
        self.tenant = sales.tenant

    def workspace(self):
        settings = SalesSettings.model_validate(self.sales.settings())
        return {
            "buying_cases": [
                {"id": key, "policy_version": version}
                for key, version in sorted(settings.active_policy_versions.items())
            ],
            "research_enabled": settings.research_enabled,
            "discovery_mode": settings.discovery_mode,
            "daily_company_limit": settings.discovery_daily_limit,
            "review_backlog_limit": settings.review_backlog_limit,
            "monthly_limit_units": settings.monthly_limit_units,
            "daily_limit_units": settings.daily_limit_units,
            "timezone": settings.timezone,
            "weekday_window": [settings.discovery_start_hour, settings.discovery_end_hour],
            "currency_units": "USD millionths; shared tenant limits",
        }

    def create(self, data, actor):
        spec = ResearchRequest.model_validate(data).model_dump(mode="json")
        if not actor or not actor.startswith(("operator:", "agent:")):
            raise Conflict("Authenticated request actor required")
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.tenant + ":discovery",),
            )
            cur.execute(
                "SELECT * FROM sales_requests WHERE tenant_id=%s AND request_key=%s",
                (self.tenant, spec["request_key"]),
            )
            row = cur.fetchone()
            if row:
                if row["config"] != spec:
                    raise Conflict("Request key is already bound to a different brief")
                return dict(row)
            cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s", (self.tenant,))
            row = cur.fetchone()
            version = (
                (row["config"] if row else {})
                .get("active_policy_versions", {})
                .get(spec["buying_case"])
            )
            if not version:
                raise Conflict("Select a published active qualification policy for this request")
            cur.execute(
                "INSERT INTO sales_requests(tenant_id,id,request_key,config,policy_version,created_by) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                (self.tenant, str(uuid4()), spec["request_key"], Json(spec), version, actor),
            )
            row = dict(cur.fetchone())
            self.sales.ops.audit(
                cur,
                row["id"],
                "request.created",
                actor,
                {"target_companies": spec["target_companies"]},
            )
            return row

    def require_open(self, request_id, *, cur):
        cur.execute(
            "SELECT r.*,s.config AS settings FROM sales_requests r LEFT JOIN sales_settings s ON s.tenant_id=r.tenant_id WHERE r.tenant_id=%s AND r.id=%s",
            (self.tenant, request_id),
        )
        row = cur.fetchone()
        if not row or row["status"] != "active":
            raise Conflict("Research request is paused, cancelled or missing")
        if (row["settings"] or {}).get("active_policy_versions", {}).get(
            row["config"]["buying_case"]
        ) != row["policy_version"]:
            raise Conflict("Research request qualification policy changed; review a new brief")
        return dict(row)

    def guard(self, payload, *, cur):
        request_id = payload.get("request_id")
        if not request_id and payload.get("prospect_id"):
            cur.execute(
                "SELECT request_id FROM sales_request_members WHERE tenant_id=%s AND prospect_id=%s",
                (self.tenant, payload["prospect_id"]),
            )
            row = cur.fetchone()
            request_id = row["request_id"] if row else None
        if request_id:
            return self.require_open(request_id, cur=cur)
        return None

    def attach(self, request_id, prospect_id, *, cur):
        request = self.require_open(request_id, cur=cur)
        cur.execute(
            "SELECT count(*) AS n FROM sales_request_members WHERE tenant_id=%s AND request_id=%s",
            (self.tenant, request_id),
        )
        if cur.fetchone()["n"] >= request["config"]["target_companies"]:
            raise Conflict("Research request company target reached")
        cur.execute(
            "INSERT INTO sales_request_members(tenant_id,request_id,prospect_id) VALUES(%s,%s,%s)",
            (self.tenant, request_id, prospect_id),
        )

    def change(self, request_id, status, expected_revision, actor, reason):
        operator(actor)
        change = RequestChange(status=status, expected_revision=expected_revision, reason=reason)
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.tenant + ":discovery",),
            )
            cur.execute(
                "UPDATE sales_requests SET status=%s,revision=revision+1,updated_at=now() WHERE tenant_id=%s AND id=%s AND revision=%s AND status<>'cancelled' RETURNING id",
                (status, self.tenant, request_id, change.expected_revision),
            )
            if not cur.fetchone():
                raise Conflict("Research request changed or was cancelled; reload before deciding")
            if status != "active":
                cur.execute(
                    "SELECT prospect_id FROM sales_request_members WHERE tenant_id=%s AND request_id=%s",
                    (self.tenant, request_id),
                )
                for row in cur.fetchall():
                    prospect = str(row["prospect_id"])
                    cur.execute(
                        "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND payload->>'prospect_id'=%s AND status IN ('review','approved')",
                        (self.tenant, prospect),
                    )
                    self.sales.ops.enqueue(
                        "sales.stop", str(uuid4()), {"prospect_id": prospect}, cur=cur
                    )
            self.sales.ops.audit(
                cur,
                request_id,
                "request." + status,
                actor,
                {"reason": reason, "previous_revision": expected_revision},
            )
        return self.get(request_id)

    def get(self, request_id):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT * FROM sales_requests WHERE tenant_id=%s AND id=%s",
                (self.tenant, request_id),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Research request not found")
            result = dict(row)
            cur.execute(
                "SELECT p.id,p.name,p.domain,p.status,p.qualification FROM sales_request_members m JOIN sales_prospects p ON p.id=m.prospect_id AND p.tenant_id=m.tenant_id WHERE m.tenant_id=%s AND m.request_id=%s ORDER BY m.created_at,p.id",
                (self.tenant, request_id),
            )
            members = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT id,kind,status,error,result FROM operation_jobs WHERE tenant_id=%s AND (payload->>'request_id'=%s OR payload->>'prospect_id'=ANY(%s)) ORDER BY created_at,id",
                (self.tenant, str(request_id), [str(p["id"]) for p in members]),
            )
            jobs = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT COALESCE(sum(spent),0) AS spent,COALESCE(sum(held),0) AS held FROM (SELECT dedup_key,max(COALESCE(actual_units,0)) AS spent,max(CASE WHEN actual_units IS NULL THEN reserved_units ELSE 0 END) AS held FROM operation_reservations WHERE tenant_id=%s AND split_part(dedup_key,':',1)=ANY(%s) GROUP BY dedup_key) costs",
                (self.tenant, [str(j["id"]) for j in jobs]),
            )
            costs = cur.fetchone()
            scouts = [j for j in jobs if j["kind"] == "sales.scout"]
            assessed = sum(bool(p["qualification"]) for p in members)
            remaining = max(0, result["config"]["target_companies"] - len(members))
            max_batches = ceil(result["config"]["target_companies"] / 20) * 5
            cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s", (self.tenant,))
            settings_row = cur.fetchone()
            settings = settings_row["config"] if settings_row else {}
            phase = (
                result["status"]
                if result["status"] != "active"
                else "complete"
                if not remaining and assessed == len(members)
                else "needs_attention"
                if len(scouts) >= max_batches and remaining
                else "researching"
                if not remaining
                else "discovering"
            )
            if result["status"] == "active" and phase != "complete":
                if (
                    settings.get("active_policy_versions", {}).get(result["config"]["buying_case"])
                    != result["policy_version"]
                ):
                    phase = "policy_changed"
                elif not settings.get("research_enabled"):
                    phase = "waiting_for_research"
                elif any(j["status"] == "failed" for j in jobs):
                    phase = "needs_attention"
            result.update(
                progress={
                    "discovered": len(members),
                    "assessed": assessed,
                    "remaining": remaining,
                    "phase": phase,
                    "search_batches": len(scouts),
                    "max_search_batches": max_batches,
                    "failed_jobs": sum(j["status"] == "failed" for j in jobs),
                    "spent_units": int(costs["spent"]),
                    "reserved_units": int(costs["held"]),
                },
                prospects=members,
                jobs=jobs,
            )
            return result

    def list(self, after=None):
        with self.sales.ops.transaction() as cur:
            boundary = None
            if after:
                cur.execute(
                    "SELECT created_at FROM sales_requests WHERE tenant_id=%s AND id=%s",
                    (self.tenant, after),
                )
                row = cur.fetchone()
                if not row:
                    raise Conflict("Request cursor not found")
                boundary = row["created_at"]
            cur.execute(
                "SELECT * FROM sales_requests WHERE tenant_id=%s AND (%s::timestamptz IS NULL OR (created_at,id)<(%s,%s::uuid)) ORDER BY created_at DESC,id DESC LIMIT 51",
                (self.tenant, boundary, boundary, after),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return {
                "items": rows[:50],
                "next_cursor": str(rows[49]["id"]) if len(rows) > 50 else None,
            }

    def allocate(self, cur, settings, local, room, prefix, end):
        cur.execute(
            "SELECT * FROM sales_requests WHERE tenant_id=%s AND status='active' ORDER BY created_at,id",
            (self.tenant,),
        )
        requests = [dict(r) for r in cur.fetchall()]
        used = 0
        for request in requests:
            if (
                settings.active_policy_versions.get(request["config"]["buying_case"])
                != request["policy_version"]
            ):
                continue
            request_id = str(request["id"])
            cur.execute(
                "SELECT count(*) AS n FROM sales_request_members WHERE tenant_id=%s AND request_id=%s",
                (self.tenant, request_id),
            )
            remaining = request["config"]["target_companies"] - cur.fetchone()["n"]
            cur.execute(
                "SELECT payload,status,deadline FROM operation_jobs WHERE tenant_id=%s AND kind='sales.scout' AND payload->>'request_id'=%s",
                (self.tenant, request_id),
            )
            jobs = list(cur.fetchall())
            remaining -= sum(
                j["payload"].get("max_companies", 20)
                for j in jobs
                if j["status"] in {"pending", "running"} and j["deadline"] > datetime.now(UTC)
            )
            batches = ceil(request["config"]["target_companies"] / 20) * 5 - len(jobs)
            index = 0
            while remaining > 0 and room > 0 and batches > 0:
                limit = min(20, room, remaining)
                payload = {
                    "request_id": request_id,
                    "segment": {
                        "id": request_id,
                        "buying_case": request["config"]["buying_case"],
                        "query": request["config"]["query"],
                    },
                    "max_companies": limit,
                    "discovery_date": str(local.date()),
                }
                job = self.sales.ops.enqueue(
                    "sales.scout", prefix + request_id + ":" + str(index), payload, cur=cur
                )
                cur.execute(
                    "UPDATE operation_jobs SET deadline=%s WHERE tenant_id=%s AND id=%s",
                    (end, self.tenant, job),
                )
                room -= limit
                remaining -= limit
                used += limit
                batches -= 1
                index += 1
        return used
