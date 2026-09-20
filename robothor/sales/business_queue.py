"""Bounded native polling of explicitly configured business source services.

Adapters are tenant-bound factories contributed through ``genus.services`` as
``sales.business.<source>``. Installation alone never enables provider reads.
Each tick advances one page; failed chains stay visible for operator recovery.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from robothor.operations.store import Conflict, digest
from robothor.sales.business import BusinessObservations
from robothor.sales.business_models import BusinessScan
from robothor.sales.models import SalesSettings
from robothor.sales.providers import RateLimited


def _configuration(sales, cur):
    # Shared lock fences pause/account changes through the page transaction.
    cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s FOR SHARE", (sales.tenant,))
    row = cur.fetchone()
    return SalesSettings.model_validate(row["config"] if row else {})


def _source(settings, scan):
    if settings.outcomes_enabled:
        for source in settings.business_sources:
            if source.source == scan.source and source.account_id == scan.account_id:
                return source
    raise Conflict("Business source disabled or account configuration changed")


class BusinessPlanner:
    def __init__(self, sales, *, clock=None):
        self.sales = sales
        self.clock = clock or (lambda: datetime.now(UTC))

    def plan(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.sales.tenant + ":business-plan",),
            )
            settings = _configuration(self.sales, cur)
            if not settings.outcomes_enabled:
                return 0
            planned = 0
            for source in settings.business_sources:
                scopes: list[tuple[Literal["practice", "signup", "order"], str | None]] = [
                    ("practice", None),
                    ("signup", None),
                ]
                cur.execute(
                    "SELECT o.external_id FROM sales_customer_bindings b JOIN sales_business_observations o "
                    "ON o.tenant_id=b.tenant_id AND o.id=b.observation_id "
                    "WHERE b.tenant_id=%s AND b.status='confirmed' AND o.source=%s AND o.account_id=%s "
                    "AND o.kind='practice' ORDER BY o.external_id",
                    (self.sales.tenant, source.source, source.account_id),
                )
                scopes.extend(("order", row["external_id"]) for row in cur.fetchall())
                for kind, practice_id in scopes:
                    cur.execute(
                        "SELECT status,result,updated_at FROM operation_jobs WHERE tenant_id=%s AND kind='sales.business' "
                        "AND payload->>'source'=%s AND payload->>'account_id'=%s AND payload->>'kind'=%s "
                        "AND (payload->>'practice_id') IS NOT DISTINCT FROM %s ORDER BY created_at DESC,id DESC LIMIT 1",
                        (self.sales.tenant, source.source, source.account_id, kind, practice_id),
                    )
                    last = cur.fetchone()
                    if last and (
                        last["status"] != "completed"
                        or not (last["result"] or {}).get("final")
                        or last["updated_at"]
                        > self.clock() - timedelta(seconds=source.refresh_seconds)
                    ):
                        continue
                    payload = BusinessScan(
                        source=source.source,
                        account_id=source.account_id,
                        kind=kind,
                        practice_id=practice_id,
                        after=None,
                        seen_cursors=[],
                        scan_id=str(uuid4()),
                    ).model_dump(mode="json")
                    job_id = self.sales.ops.enqueue(
                        "sales.business", digest(payload), payload, cur=cur
                    )
                    self.sales.ops.audit(
                        cur,
                        job_id,
                        "business.scan_planned",
                        detail={"source": source.source, "kind": kind},
                    )
                    planned += 1
                    if planned >= 25:
                        return planned
            return planned


class BusinessWorker:
    def __init__(self, sales):
        self.sales = sales

    def _admit(self, job, scan):
        with self.sales.ops.transaction() as cur:
            settings = _configuration(self.sales, cur)
            source = _source(settings, scan)
            cur.execute(
                "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND id=%s AND lease_token=%s "
                "AND status='running' AND lease_until>clock_timestamp() AND deadline>clock_timestamp()",
                (self.sales.tenant, job["id"], job["lease_token"]),
            )
            if not cur.fetchone():
                raise Conflict("Business read lease expired or replaced")
            self._practice(cur, scan)
            return source

    def _practice(self, cur, scan):
        if scan.kind != "order":
            return
        cur.execute(
            "SELECT 1 FROM sales_customer_bindings b JOIN sales_business_observations o "
            "ON o.tenant_id=b.tenant_id AND o.id=b.observation_id "
            "WHERE b.tenant_id=%s AND b.status='confirmed' AND o.source=%s AND o.account_id=%s "
            "AND o.kind='practice' AND o.external_id=%s FOR SHARE OF b,o",
            (self.sales.tenant, scan.source, scan.account_id, scan.practice_id),
        )
        if not cur.fetchone():
            raise Conflict("Order import requires a currently reviewed practice")

    def _commit(self, job, scan, source, page):
        with self.sales.ops.transaction() as cur:
            if _source(_configuration(self.sales, cur), scan) != source:
                raise Conflict("Business source configuration changed during read")
            # Same lock order as observation/binding writes; then hold the
            # reviewed association until the page and cursor are committed.
            business = BusinessObservations(self.sales)
            business._lock(cur, scan.source, scan.account_id)
            self._practice(cur, scan)
            business.commit_page(job, page, cur=cur)

    async def tick(self):
        settings = SalesSettings.model_validate(await asyncio.to_thread(self.sales.settings))
        if not settings.outcomes_enabled or not settings.business_sources:
            return False
        await asyncio.to_thread(BusinessPlanner(self.sales).plan)
        job = await asyncio.to_thread(self.sales.ops.claim, "sales.business", lease_seconds=120)
        if not job:
            return False
        try:
            scan = BusinessScan.model_validate(job["payload"])
            source = await asyncio.to_thread(self._admit, job, scan)
            from robothor.engine.services import get_service

            factory = await asyncio.to_thread(get_service, "sales.business." + scan.source)
            if not callable(factory):
                raise Conflict("Business source service is not installed or enabled")
            adapter = await asyncio.to_thread(factory, self.sales.tenant)
            if not callable(getattr(adapter, "business_page", None)):
                raise Conflict("Business source service contract mismatch")
            allowed = await asyncio.to_thread(
                self.sales.ops.admit_request,
                "business:" + scan.source + ":" + scan.account_id,
                limit=20,
                window_seconds=60,
            )
            if not allowed:
                raise RateLimited(60)
            page = await asyncio.wait_for(adapter.business_page(scan.model_dump(mode="json")), 75)
            await asyncio.to_thread(self._commit, job, scan, source, page)
        except RateLimited as exc:
            await self._defer(
                job, "Business provider read rate limited", exc.retry_after, busy=True
            )
        except Exception:  # noqa: BLE001 - private adapter diagnostics must not reach workflow logs
            # Provider payloads and exceptions may contain business data or
            # credentials; only this fixed diagnostic reaches the work ledger.
            await self._defer(job, "Business provider page requires review", 300)
        return True

    async def _defer(self, job, reason, delay, *, busy=False):
        # After lease loss, only the replacement worker may change the job.
        with suppress(Conflict):
            await asyncio.to_thread(
                self.sales.ops.defer,
                job["id"],
                job["lease_token"],
                reason,
                delay_seconds=delay,
                busy=busy,
            )
