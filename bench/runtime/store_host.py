"""Experimental lifecycle host using Robothor's existing run and control stores.

The required gateway factory is a trusted service: it must validate principal,
options and tool authority. This module never chooses business tools or models.
"""

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from psycopg2.extras import Json

from bench.runtime.candidates import admit_gateway
from robothor.db.connection import get_connection
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.engine.runtime import controls
from robothor.engine.runtime.contracts import RuntimeResult, RuntimeStoppedError, Usage
from robothor.engine.runtime.deadlines import require_time


class RunGateway:
    def __init__(self, run, host):
        self.run, self.host = run, host

    @property
    def schemas(self):
        return self.host.schemas

    @property
    def verified(self):
        return self.host.verified

    async def admit(self, tenant):
        require_time()
        if tenant != self.run.tenant_id:
            raise ValueError("run gateway tenant mismatch")
        if await asyncio.to_thread(controls.stopped, tenant, self.run.id):
            raise RuntimeStoppedError("durable stop denies candidate execution")
        await admit_gateway(self.host, tenant)

    async def invoke(self, tenant, name, arguments):
        await self.admit(tenant)
        return await self.host.invoke(tenant, name, arguments)


class StoreHost:
    def __init__(self, gateway_factory, *, request_token_bound=None):
        self.gateway_factory = gateway_factory
        self.request_token_bound = request_token_bound
        self._admissions = set()

    async def prepare(self, request, identity):
        context = request.context
        require_time(context)
        from bench.runtime.goal_binding import bind_goal

        ledger = await bind_goal(context, self.request_token_bound)
        gateway = await self.gateway_factory(request)
        if ledger is not None:
            from bench.runtime.goal_gateway import GoalGateway

            gateway = GoalGateway(gateway, ledger)
        await admit_gateway(gateway, context.tenant_id)
        if await asyncio.to_thread(controls.stopped, context.tenant_id, ""):
            raise ValueError("request already stopped")
        if context.parent_id and await asyncio.to_thread(
            controls.stopped, context.tenant_id, context.parent_id
        ):
            raise ValueError("parent run already stopped")
        run = AgentRun(
            tenant_id=context.tenant_id,
            user_id=context.principal_id,
            agent_id=request.agent_id,
            trigger_type=TriggerType(request.options.get("trigger_type", TriggerType.MANUAL)),
            correlation_id=context.request_id,
            parent_run_id=context.parent_id,
            started_at=datetime.now(UTC),
            trigger_detail=f"goal:{context.goal_id}" if context.goal_id else None,
        )
        metadata = {**asdict(context), **asdict(identity)}
        metadata["deadline"] = context.deadline.isoformat() if context.deadline else None
        insertion = self._track(asyncio.to_thread(self._insert_and_attach, run, metadata))
        try:
            await asyncio.shield(insertion)
        except asyncio.CancelledError:
            self._track(self._cancel_admission(insertion, run, identity, context.request_id))
            raise
        return run, RunGateway(run, gateway)

    def _insert_and_attach(self, run, metadata):
        from robothor.goals.runtime import attach_run

        self._insert(run, metadata)
        attach_run(run)

    async def bind_candidate(self, request, candidate):
        from bench.runtime.goal_binding import bind_candidate

        return bind_candidate(request.context, candidate, self.request_token_bound)

    def _track(self, work):
        task = asyncio.create_task(work)
        self._admissions.add(task)

        def completed(done):
            self._admissions.discard(done)
            if not done.cancelled() and done.exception():
                logging.getLogger(__name__).error(
                    "Candidate admission reconciliation failed: %s", done.exception()
                )

        task.add_done_callback(completed)
        return task

    async def drain_admissions(self):
        """Shutdown/test hook: retain reconciliation work until it has finished."""
        while self._admissions:
            await asyncio.gather(*tuple(self._admissions), return_exceptions=True)

    async def _cancel_admission(self, insertion, run, identity, request_id):
        await asyncio.to_thread(
            controls.issue_request,
            run.tenant_id,
            request_id,
            "Cancelled during candidate admission",
        )
        try:
            await insertion
        except Exception:
            # A lost commit acknowledgement is uncertain; attempt terminal reconciliation.
            logging.getLogger(__name__).debug("Admission insert failed", exc_info=True)
        run.status = RunStatus.CANCELLED
        run.completed_at = datetime.now(UTC)
        run.duration_ms = round((run.completed_at - run.started_at).total_seconds() * 1000)
        run.error_message = "Cancelled during admission; no model or business action dispatched"
        await self.finish(RuntimeResult(run, Usage(0, 0, 0, 0.0), False, False, identity))

    @staticmethod
    def _insert(run, metadata):
        # Legacy correlation_id is UUID; textual request IDs remain in runtime_context.
        try:
            correlation = str(UUID(run.correlation_id))
        except ValueError:
            correlation = None
        with get_connection() as conn, conn.cursor() as cur:
            if run.parent_run_id:
                cur.execute(
                    "SELECT 1 FROM agent_runs WHERE tenant_id=%s AND id=%s",
                    (run.tenant_id, run.parent_run_id),
                )
                if not cur.fetchone():
                    raise ValueError("parent run not found in tenant")
            cur.execute(
                """INSERT INTO agent_runs
                   (id,tenant_id,user_id,agent_id,trigger_type,trigger_detail,correlation_id,parent_run_id,
                    status,started_at,runtime_context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                    'running',%s,%s)""",
                (
                    run.id,
                    run.tenant_id,
                    run.user_id,
                    run.agent_id,
                    str(run.trigger_type),
                    run.trigger_detail,
                    correlation,
                    run.parent_run_id,
                    run.started_at,
                    Json(metadata),
                ),
            )

    async def finish(self, result):
        await asyncio.to_thread(self._finish, result)

    @staticmethod
    def _finish(result):
        run = result.run
        metadata = {
            "usage": asdict(result.usage),
            "unresolved": result.unresolved,
            "verified": result.verified,
        }
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_runs SET status=%s,completed_at=%s,duration_ms=%s,
                   input_tokens=%s,output_tokens=%s,total_cost_usd=%s,output_text=%s,
                   error_message=%s,verified_status=%s,runtime_context=runtime_context || %s
                   WHERE tenant_id=%s AND id=%s AND runtime_context->>'runtime_id'=%s
                   AND runtime_context->>'runtime_version'=%s
                   AND runtime_context->>'checkpoint_version'=%s AND status='running' RETURNING id""",
                (
                    str(run.status),
                    run.completed_at,
                    run.duration_ms,
                    result.usage.input_tokens,
                    result.usage.output_tokens,
                    result.usage.cost_usd,
                    run.output_text,
                    run.error_message,
                    run.verified_status,
                    Json(metadata),
                    run.tenant_id,
                    run.id,
                    result.runtime.runtime_id,
                    result.runtime.runtime_version,
                    str(result.runtime.checkpoint_version),
                ),
            )
            if not cur.fetchone():
                raise ValueError("candidate run identity unavailable for finalization")

    async def recover_expired(self, tenant):
        from bench.runtime.recovery import recover_expired

        recovered = await asyncio.to_thread(recover_expired, tenant)
        if recovered:
            await asyncio.to_thread(controls.signal_stopped, tenant, "Expired candidate worker")
        return recovered

    async def control(self, tenant, run_id, action, note):
        return await asyncio.to_thread(controls.issue, tenant, run_id, action, note)
