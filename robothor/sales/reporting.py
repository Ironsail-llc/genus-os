"""Bounded aggregate feedback, immutable native analyst reports and proposed changes."""

import asyncio
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field

from robothor.operations.store import BudgetExceeded, Conflict, digest
from robothor.sales.models import Contract, SalesSettings
from robothor.sales.runtime import ResearchWorker


class Proposal(Contract):
    proposal: str = Field(min_length=1, max_length=2000)
    metric_paths: list[str] = Field(min_length=1, max_length=10)


class Analysis(Contract):
    cohort_summary: str = Field(min_length=1, max_length=6000)
    limitations: list[str] = Field(min_length=1, max_length=30)
    proposed_changes: list[Proposal] = Field(max_length=10)
    supporting_counts: dict[str, Annotated[int, Field(strict=True, ge=0)] | None]


def validate_analysis(output, dataset):
    if output.supporting_counts != dataset["supporting_counts"]:
        raise Conflict("Analyst changed measured supporting counts")
    for proposal in output.proposed_changes:
        for path in proposal.metric_paths:
            value = dataset
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise Conflict("Analyst proposal cites a missing metric")
                value = value[part]
            if isinstance(value, (dict, list)):
                raise Conflict("Analyst proposals must cite specific measured values")


class Reports:
    MAX_COMPANIES = 1000

    def __init__(self, sales):
        self.sales, self.tenant = sales, sales.tenant

    def capture(self):
        started = datetime.now(UTC)
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM sales_prospects WHERE tenant_id=%s", (self.tenant,)
            )
            total = cur.fetchone()["n"]
            cur.execute(
                "SELECT id,status,qualification,dossier,created_at FROM sales_prospects WHERE tenant_id=%s ORDER BY created_at,id LIMIT %s",
                (self.tenant, self.MAX_COMPANIES),
            )
            prospects = [dict(r) for r in cur.fetchall()]
            prospect_ids = [str(p["id"]) for p in prospects]
            cur.execute(
                "SELECT prospect_id,direction,count(*) AS n FROM sales_messages WHERE tenant_id=%s AND prospect_id::text=ANY(%s) GROUP BY prospect_id,direction",
                (self.tenant, prospect_ids),
            )
            messages = {(str(r["prospect_id"]), r["direction"]): r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT coalesce(sum(actual),0) AS actual,coalesce(sum(held),0) AS held FROM (SELECT dedup_key,max(coalesce(actual_units,0)) AS actual,max(CASE WHEN actual_units IS NULL THEN reserved_units ELSE 0 END) AS held FROM operation_reservations WHERE tenant_id=%s AND scope LIKE 'sales:%%' GROUP BY dedup_key) cost",
                (self.tenant,),
            )
            costs = dict(cur.fetchone())
            cur.execute(
                "SELECT source,account_id,kind,count(*) AS observations,max(observed_at) AS observed_through FROM sales_business_observations WHERE tenant_id=%s GROUP BY source,account_id,kind ORDER BY source,account_id,kind",
                (self.tenant,),
            )
            sources = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT kind,status,count(*) AS n FROM operation_jobs WHERE tenant_id=%s AND kind LIKE 'sales.%%' GROUP BY kind,status",
                (self.tenant,),
            )
            work = [dict(r) for r in cur.fetchall()]
        # Use the same current-observation/attribution rules as the prospect UI.
        # Sampling and collection timestamps remain explicit; this is not a claim
        # that an API page proves complete historical retention.
        retention = {str(p["id"]): self.sales.retention(p["id"], started) for p in prospects}

        def counts(rows):
            observed = [retention[str(p["id"])] for p in rows]
            eligible = [
                r
                for r in observed
                if r.get("coverage_complete") is True and r.get("repeat_within_30_days") is not None
            ]
            return {
                "observed_businesses": len(rows),
                "qualified_businesses": sum(
                    (p["qualification"] or {}).get("decision") == "qualified" for p in rows
                ),
                "accepted_businesses": sum(
                    p["status"] in {"accepted", "promoted", "engaged", "onboarding", "active"}
                    for p in rows
                ),
                "contacted_businesses": sum((str(p["id"]), "outbound") in messages for p in rows),
                "replied_businesses": sum((str(p["id"]), "inbound") in messages for p in rows),
                "first_fulfilled": sum(
                    bool(r.get("first_completed_order")) and not r.get("requires_review")
                    for r in observed
                ),
                "repeat_fulfilled_businesses": sum(
                    (r.get("completed_orders") or 0) > 1 and not r.get("requires_review")
                    for r in observed
                ),
                "eligible_30_day": len(eligible),
                "retained_30_day": sum(r["repeat_within_30_days"] is True for r in eligible)
                if eligible
                else None,
                "held_business_attribution": sum(
                    r.get("requires_review") is True for r in observed
                ),
            }

        grouped = defaultdict(list)
        for p in prospects:
            qualification = p["qualification"] or {}
            grouped[
                (
                    qualification.get("buying_case")
                    or (p["dossier"] or {}).get("buying_case")
                    or "unassigned",
                    qualification.get("policy_version") or "unassessed",
                    p["created_at"].strftime("%Y-%m"),
                )
            ].append(p)
        from robothor.sales.calibration import Calibration

        calibration = Calibration(self.sales)
        cohort_page = calibration.list_cohorts()
        assessments = []
        for cohort in cohort_page["items"][:20]:
            result = calibration.report(cohort["id"])
            assessments.append(
                {
                    "name": cohort["name"],
                    "target_size": cohort["target_size"],
                    "initial": result["initial"],
                    "latest": result["latest"],
                    "corrections": result["corrections"],
                    "review_complete": result["review_complete"],
                    "agreement_target_met": result["agreement_target_met"],
                }
            )
        result = {
            "collection_started_at": started.isoformat(),
            "collection_finished_at": datetime.now(UTC).isoformat(),
            "population": {
                "known_businesses": total,
                "observed_businesses": len(prospects),
                "complete": total == len(prospects),
                "selection": "oldest_created_first",
                "limit": self.MAX_COMPANIES,
            },
            "supporting_counts": counts(prospects),
            "current_statuses": dict(Counter(p["status"] for p in prospects)),
            "cohorts": [
                {
                    "buying_case": case,
                    "policy_version": policy,
                    "discovery_month": month,
                    **counts(rows),
                }
                for (case, policy, month), rows in sorted(grouped.items())
            ],
            "costs": {
                "actual_units": int(costs["actual"]),
                "reserved_units": int(costs["held"]),
                "scope": "all recorded tenant sales reservations; USD millionths; deduplicated across budget scopes",
            },
            "business_sources": sources,
            "work": work,
            "qualification_reviews": assessments,
            "qualification_reviews_complete": len(cohort_page["items"]) <= 20
            and not cohort_page["next_cursor"],
            "limitations": [
                "Counts describe observed records and current acceptance, not all businesses in the market or a completed customer pilot.",
                "Individual fulfilled-order evidence does not prove complete historical coverage; incomplete or immature retention is unknown.",
                "Cohorts are observational, not randomized. Different policies and discovery months are separated; small samples do not establish causality.",
                "Costs are recorded sales operations only, excluding SaaS subscriptions, invoice adjustments, revenue and product cost.",
                "A report is collected over the stated interval. Data arriving afterward belongs to a later report. Proposals require human review.",
            ],
        }
        if total > len(prospects):
            result["limitations"].append(
                "Company sample is truncated; do not extrapolate sampled conversion rates to the whole tenant."
            )
        return json.loads(json.dumps(result, default=str))

    def plan(self):
        settings = SalesSettings.model_validate(self.sales.settings())
        if not settings.research_enabled or not settings.agents.get("analyst"):
            return None
        period = datetime.now(UTC).strftime("%G-W%V")
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.tenant + ":sales-report",),
            )
            cur.execute(
                "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND kind='sales.analyst' AND dedup_key=%s",
                (self.tenant, period),
            )
            if cur.fetchone():
                return None
            dataset = self.capture()
            if not dataset["supporting_counts"]["observed_businesses"]:
                return None
            return self.sales.ops.enqueue(
                "sales.analyst",
                period,
                {"period": period, "dataset": dataset, "dataset_hash": digest(dataset)},
                cur=cur,
            )

    def get(self, report_id):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT id,status,result,error,created_at FROM operation_jobs WHERE tenant_id=%s AND id=%s AND kind='sales.analyst'",
                (self.tenant, report_id),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Sales report not found")
            if row["status"] != "completed":
                return {"id": str(row["id"]), "status": row["status"], "error": row["error"]}
            return {"id": str(row["id"]), "status": row["status"], **row["result"]}

    def latest(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT id FROM operation_jobs WHERE tenant_id=%s AND kind='sales.analyst' AND status='completed' ORDER BY created_at DESC,id DESC LIMIT 1",
                (self.tenant,),
            )
            row = cur.fetchone()
        return self.get(row["id"]) if row else None

    def list(self, after=None):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT id,status,error,created_at,payload->>'period' AS period FROM operation_jobs WHERE tenant_id=%s AND kind='sales.analyst' AND (%s IS NULL OR id>%s::uuid) ORDER BY id LIMIT 51",
                (self.tenant, after, after),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return {"items": rows[:50], "next_cursor": str(rows[49]["id"]) if len(rows) > 50 else None}


class AnalystWorker(ResearchWorker):
    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.research_enabled:
            return False
        await asyncio.to_thread(Reports(self.sales).plan)
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.analyst", lease_seconds=360)
        if not job:
            return False
        try:
            dataset = job["payload"]["dataset"]
            if digest(dataset) != job["payload"]["dataset_hash"]:
                raise Conflict("Sales report dataset changed")
            output, context, run_id = await self._generate(
                job,
                settings,
                "analyst",
                Analysis,
                {"dataset": dataset},
                "Analyze the supplied aggregate measurements. Copy supporting_counts exactly; explain missing and immature coverage. Cite existing scalar metric_paths in any proposed changes. Never activate a policy, change a budget, invent a conversion or send a message. Return Analysis JSON.",
            )
            await asyncio.to_thread(self.commit, job, output, context, run_id)
        except (Conflict, BudgetExceeded, ValueError) as exc:
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                str(exc) if isinstance(exc, Conflict) else "Invalid or unfunded sales analysis",
                delay_seconds=300,
            )
        return True

    def commit(self, job, output, context, run_id):
        dataset = context["dataset"]
        if digest(dataset) != job["payload"]["dataset_hash"]:
            raise Conflict("Analysis checkpoint does not match its fixed dataset")
        validate_analysis(output, dataset)
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR SHARE",
                (self.sales.tenant,),
            )
            if not cur.fetchone()["config"].get("research_enabled"):
                raise Conflict("Analysis paused during generation")
            result = {
                "dataset": dataset,
                "dataset_hash": digest(dataset),
                "analysis": output.model_dump(mode="json"),
                "run_id": run_id,
                "prepared_at": datetime.now(UTC).isoformat(),
            }
            self.sales.ops.complete(job["id"], job["lease_token"], result, cur=cur)
            self.sales.ops.audit(
                cur,
                job["id"],
                "analysis.prepared",
                detail={
                    "dataset_hash": result["dataset_hash"],
                    "proposals": len(output.proposed_changes),
                },
            )
