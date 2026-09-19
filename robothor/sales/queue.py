"""Deterministic queue pumps called by native service workflows, never by an LLM.

No scheduler or background process is installed on import. Each workflow tick
advances one stage under its tenant's explicit binding. Independent control and
conversation workflows do not wait for the research workflow to finish.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from robothor.operations.gates import run_shared
from robothor.operations.store import Conflict
from robothor.sales.business_queue import BusinessWorker
from robothor.sales.delivery import DeliveryWorker, StopWorker
from robothor.sales.ingestion import InstantlyInboxWorker
from robothor.sales.models import SalesSettings
from robothor.sales.promotion import PromotionWorker
from robothor.sales.reconciliation import ReconciliationWorker
from robothor.sales.runtime import DraftWorker, ResearchWorker
from robothor.sales.stages import ActivationWorker, ContactWorker, ConversationWorker, ScoutWorker
from robothor.sales.verification import VerificationWorker


class QueueDriver:
    def __init__(self, sales):
        self.sales = sales

    async def tick(self, stage, workflow_id):
        return await run_shared(
            self.sales.ops, "sales-fleet", lambda: self._tick(stage, workflow_id)
        )

    async def _tick(self, stage, workflow_id):
        from robothor.sales.deployment import assert_queue_open

        await asyncio.to_thread(assert_queue_open, self.sales)
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if settings.workflow_bindings.get(stage) != workflow_id:
            raise Conflict("Sales stage is not bound to this native workflow")
        if stage == "plan":
            worked = await asyncio.to_thread(DiscoveryPlanner(self.sales).plan)
        else:
            workers = {
                "scout": (ScoutWorker, "tick"),
                "research": (ResearchWorker, "tick"),
                "qualify": (ResearchWorker, "qualify_tick"),
                "contacts": (ContactWorker, "tick"),
                "verify": (VerificationWorker, "tick"),
                "promotion": (PromotionWorker, "tick"),
                "draft": (DraftWorker, "tick"),
                "conversation": (ConversationWorker, "tick"),
                "activation": (ActivationWorker, "tick"),
                "delivery": (DeliveryWorker, "tick"),
                "stop": (StopWorker, "tick"),
                "inbox": (InstantlyInboxWorker, "tick"),
                "reconcile": (ReconciliationWorker, "drain"),
                "business": (BusinessWorker, "tick"),
            }
            if stage not in workers:
                raise Conflict("Unknown sales queue stage")
            factory, method = workers[stage]
            worked = await getattr(factory(self.sales), method)()
        return {"stage": stage, "worked": bool(worked)}


class DiscoveryPlanner:
    """At most one daily plan; reserve review capacity before buying research."""

    def __init__(self, sales, *, clock=None):
        self.sales = sales
        self.clock = clock or (lambda: datetime.now(UTC))

    def plan(self):
        with self.sales.ops.transaction() as cur:
            # Same lock as direct discovery. Concurrent planners and manual
            # imports cannot each see and allocate the same free capacity.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.sales.tenant + ":discovery",),
            )
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            row = cur.fetchone()
            settings = SalesSettings.model_validate(row["config"] if row else {})
            local = self.clock().astimezone(ZoneInfo(settings.timezone))
            if (
                not settings.research_enabled
                or local.weekday() > 4
                or not settings.discovery_start_hour <= local.hour < settings.discovery_end_hour
            ):
                return False
            if (
                not settings.agents.get("scout")
                or not settings.discovery_segments
                or settings.daily_limit_units < ResearchWorker.RUN_ALLOWANCE_UNITS
                or settings.monthly_limit_units < ResearchWorker.RUN_ALLOWANCE_UNITS
            ):
                return False
            segments = [
                s
                for s in settings.discovery_segments
                if s.buying_case in settings.active_policy_versions
            ]
            if not segments:
                raise Conflict("Discovery segments require active qualification policies")
            prefix = "discovery:" + str(local.date()) + ":"
            cur.execute(
                "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND kind='sales.scout' AND dedup_key LIKE %s LIMIT 1",
                (self.sales.tenant, prefix + "%"),
            )
            if cur.fetchone():
                return False
            cur.execute(
                "SELECT count(*) AS n FROM sales_prospects WHERE tenant_id=%s AND created_at>=%s",
                (self.sales.tenant, local.replace(hour=0, minute=0, second=0, microsecond=0)),
            )
            daily_room = settings.discovery_daily_limit - cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM sales_prospects WHERE tenant_id=%s AND status IN ('discovered','researched','needs_research','qualified')",
                (self.sales.tenant,),
            )
            review_room = settings.review_backlog_limit - cur.fetchone()["n"]
            # Explicitly queued/manual scouts also occupy review capacity until
            # completed or expired. Unknown legacy batch size reserves 20.
            cur.execute(
                "SELECT payload FROM operation_jobs WHERE tenant_id=%s AND kind='sales.scout' AND status IN ('pending','running') AND deadline>now()",
                (self.sales.tenant,),
            )
            pending = sum(int(j["payload"].get("max_companies", 20)) for j in cur.fetchall())
            room = max(0, min(daily_room, review_room) - pending)
            if not room:
                return False
            end = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                hours=settings.discovery_end_hour
            )
            offset = local.date().toordinal() % len(segments)
            total = room
            index = 0
            while room:
                limit = min(room, 20)
                segment = segments[(offset + index) % len(segments)]
                job = self.sales.ops.enqueue(
                    "sales.scout",
                    prefix + str(index),
                    {
                        "segment": segment.model_dump(mode="json"),
                        "max_companies": limit,
                        "discovery_date": str(local.date()),
                    },
                    cur=cur,
                )
                cur.execute(
                    "UPDATE operation_jobs SET deadline=%s WHERE tenant_id=%s AND id=%s",
                    (end, self.sales.tenant, job),
                )
                room -= limit
                index += 1
            self.sales.ops.audit(
                cur,
                local.date(),
                "discovery.planned",
                detail={"jobs": index, "candidate_limit": total},
            )
            return True
