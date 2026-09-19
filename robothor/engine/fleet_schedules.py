"""Engine-loop ownership, reconciliation and verification of managed fleet cron.

The deployment adapter must hold durable admission closed while reconciling.
This component does not select settings or attest platform/plugin readiness.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.cron import CronTrigger

from robothor.engine.fleet_context import FleetInvocation, invocation
from robothor.engine.workflow import parse_workflow
from robothor.operations.store import Conflict


def _cron_identity(trigger):
    if type(trigger) is not CronTrigger:
        raise Conflict("Managed workflow trigger changed")
    return (
        tuple(str(field) for field in trigger.fields),
        str(trigger.timezone),
        trigger.start_date,
        trigger.end_date,
        trigger.jitter,
    )


class FleetSchedules:
    """Own only jobs introduced by this engine-loop instance; refuse collisions.

    Construct and call on the engine event loop. The controller must bridge to
    this loop, never inspect APScheduler concurrently from its database thread.
    A fresh process starts with no ownership and cannot adopt loose definitions.
    """

    def __init__(self, native_scheduler, *, tenant, admission_verifier=None):
        self.native = native_scheduler
        self.engine = native_scheduler.workflow_engine
        if self.engine is None or self.engine.config.tenant_id != tenant:
            raise Conflict("Managed workflow tenant does not match this engine")
        self.tenant = tenant
        self._admission_verifier = admission_verifier
        self._loop = asyncio.get_running_loop()
        self._owned = {}
        self._release_id = None
        self._generation = None

    def _on_loop(self):
        if asyncio.get_running_loop() is not self._loop:
            raise Conflict("Managed schedules require their owning event loop")
        if (
            self.native.workflow_engine is not self.engine
            or self.engine.config.tenant_id != self.tenant
        ):
            raise Conflict("Managed workflow engine identity changed")

    def reconcile(self, snapshot):
        """Reconcile reviewed definitions while durable deployment blocks admission.

        Partial mutation leaves the generation invalid. Retrying reconciles the
        complete owned set. The caller must not clear durable pending state on
        an exception. Restoring None supports only an originally empty baseline.
        """
        self._on_loop()
        desired = {}
        if snapshot is not None:
            for path in snapshot.metadata()["workflows"]:
                definition = parse_workflow(snapshot.document(path))
                if definition.id in desired or len(definition.triggers) != 1:
                    raise Conflict("Managed workflow needs one unambiguous cron trigger")
                trigger = definition.triggers[0]
                if trigger.type != "cron" or not trigger.cron:
                    raise Conflict("Managed workflow requires a cron trigger")
                cron = CronTrigger.from_crontab(trigger.cron, timezone=trigger.timezone)
                desired[definition.id] = (deepcopy(definition), cron)
        scheduler = self.native.scheduler
        for workflow_id in desired.keys() - self._owned.keys():
            if self.engine.get_workflow(workflow_id) or scheduler.get_job(
                f"workflow:{workflow_id}"
            ):
                raise Conflict("Managed workflow collides with unowned runtime state")
        self._generation = None
        # Retain ownership even if a scheduler mutation raises partway through.
        owned_ids = self._owned.keys() | desired.keys()
        self._owned = {key: desired.get(key) for key in owned_ids}
        for workflow_id in owned_ids:
            job = scheduler.get_job(f"workflow:{workflow_id}")
            if job is not None:
                job.remove()
            self.engine._workflows.pop(workflow_id, None)
        release_id = snapshot.release_id if snapshot is not None else None
        generation = str(uuid4())
        for workflow_id, (definition, cron) in desired.items():
            self.engine._workflows[workflow_id] = deepcopy(definition)
            scheduler.add_job(
                self._run,
                trigger=deepcopy(cron),
                args=[workflow_id, release_id, generation],
                id=f"workflow:{workflow_id}",
                name=f"workflow:{definition.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )
        self._owned = desired
        self._release_id = release_id
        self._generation = generation
        return generation

    def verify(self, release_id, generation):
        self._on_loop()
        if not generation or (release_id, generation) != (self._release_id, self._generation):
            raise Conflict("Managed workflow generation is retired or incomplete")
        scheduler = self.native.scheduler
        if scheduler.state != STATE_RUNNING:
            raise Conflict("Managed workflow scheduler is not running")
        expected_jobs = {f"workflow:{key}" for key in self._owned}
        if any(
            job.func == self._run and job.id not in expected_jobs for job in scheduler.get_jobs()
        ):
            raise Conflict("Managed workflow callback has an unreviewed schedule")
        for workflow_id, (definition, cron) in self._owned.items():
            actual = self.engine.get_workflow(workflow_id)
            job = scheduler.get_job(f"workflow:{workflow_id}")
            if (
                actual is None
                or asdict(actual) != asdict(definition)
                or job is None
                or job.func != self._run
                or tuple(job.args) != (workflow_id, release_id, generation)
                or job.kwargs
                or job.max_instances != 1
                or not job.coalesce
                or job.misfire_grace_time != 60
                or job.next_run_time is None
                or _cron_identity(job.trigger) != _cron_identity(cron)
            ):
                raise Conflict("Managed workflow definition or schedule drifted")
        return {
            "release_id": release_id,
            "generation": generation,
            "workflows": sorted(self._owned),
        }

    async def _run(self, workflow_id, release_id, generation):
        self.verify(release_id, generation)
        if workflow_id not in self._owned:
            raise Conflict("Managed workflow is no longer owned")
        token = invocation.set(
            FleetInvocation(
                tenant=self.tenant,
                release_id=release_id,
                workflow_id=workflow_id,
                verify_current=lambda: self._verify_admission(release_id, generation),
            )
        )
        try:
            return await self.engine.execute(
                workflow_id=workflow_id,
                trigger_type="cron",
                trigger_detail=f"fleet:{release_id}:{generation}",
                user_id=f"service:workflow:{workflow_id}",
                user_role="service",
            )
        finally:
            invocation.reset(token)

    async def _verify_admission(self, release_id, generation):
        self.verify(release_id, generation)
        if self._admission_verifier is not None:
            await self._admission_verifier(release_id, generation)
        self.verify(release_id, generation)
