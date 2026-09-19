"""Native managed-sales deployment and restart reconciliation on the engine loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from robothor.engine.fleet_schedules import FleetSchedules
from robothor.operations.store import Conflict
from robothor.sales.deployment import DeploymentCoordinator
from robothor.sales.service import operator
from robothor.templates.fleet_snapshot import load_snapshot
from robothor.templates.fleet_store import staged_release_path


def _fingerprint(record):
    keys = (
        "id",
        "tenant_id",
        "source_release_id",
        "target_release_id",
        "base_revision",
        "previous_config",
        "target_config",
        "source_artifact",
        "target_artifact",
    )
    return json.dumps({key: record[key] for key in keys}, sort_keys=True, default=str)


async def _drain_thread(function):
    """Cancellation cannot release the control lock before its transaction ends."""
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            task.exception()
        raise


class NativeSalesRuntime:
    def __init__(self, native_scheduler, sales, workspace, assets):
        self.native = native_scheduler
        self.workspace = Path(workspace)
        self.coordinator = DeploymentCoordinator(sales, self.workspace)
        self.assets = assets
        self.loop = asyncio.get_running_loop()
        self.schedules = FleetSchedules(
            native_scheduler, tenant=sales.tenant, admission_verifier=self.verify_admission
        )
        self._lock = asyncio.Lock()
        self._snapshot = None
        self._generation = None
        self._bound = None
        self._operation = None
        self._booted = False

    def _state(self):
        # One database snapshot, rather than mixing settings from before a
        # concurrent commit with the absence of its pending row afterwards.
        with self.coordinator.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT s.config,s.revision,to_jsonb(d) AS pending FROM sales_settings s "
                "LEFT JOIN sales_deployments d ON d.tenant_id=s.tenant_id AND d.status='preparing' "
                "WHERE s.tenant_id=%s",
                (self.coordinator.sales.tenant,),
            )
            row = cur.fetchone()
            return dict(row) if row else {"config": {}, "revision": 0, "pending": None}

    async def _apply(self, release_id):
        self._bound = None
        self._generation = None
        snapshot = None
        if release_id is not None:
            root = staged_release_path(self.workspace, release_id)
            snapshot = await asyncio.to_thread(load_snapshot, root, expected_digest=release_id)
            await asyncio.to_thread(self.assets.verify, snapshot, root)
        generation = self.schedules.reconcile(snapshot)
        self.schedules.verify(release_id, generation)
        self._snapshot, self._generation = snapshot, generation

    async def bootstrap(self):
        async with self._lock:
            state = await asyncio.to_thread(self._state)
            # Recovery never chooses commit versus abort on the operator's behalf.
            # Durable pending state holds admission closed after a process restart.
            release = None if state["pending"] else state["config"].get("fleet_release_id")
            await self._apply(release)
            self._booted = True

    async def _reconcile(self, transition_id, *, restoring=False):
        state = await asyncio.to_thread(self._state)
        record = state["pending"]
        if record is None or str(record["id"]) != str(transition_id):
            raise Conflict("Pending deployment does not match this request")
        if record["source_release_id"] is None and any(
            record["previous_config"].get(key) for key in ("agents", "workflow_bindings")
        ):
            raise Conflict("Existing unmanaged sales bindings need a verified migration baseline")
        release = record["source_release_id"] if restoring else record["target_release_id"]
        await self._apply(release)
        self._bound = (_fingerprint(record), restoring)
        self._booted = True
        return record

    async def reconcile(self, transition_id, *, restoring=False):
        async with self._lock:
            await self._reconcile(transition_id, restoring=restoring)
            return self.schedules.verify(
                self._snapshot.release_id if self._snapshot else None, self._generation
            )

    async def prepare(self, release_id, *, expected_revision, actor, reason):
        operator(actor)
        async with self._lock:
            state = await asyncio.to_thread(self._state)
            if state["config"].get("fleet_release_id") is None and any(
                state["config"].get(key) for key in ("agents", "workflow_bindings")
            ):
                raise Conflict(
                    "Existing unmanaged sales bindings need a verified migration baseline"
                )
            return await _drain_thread(
                lambda: self.coordinator.prepare(
                    release_id, expected_revision=expected_revision, actor=actor, reason=reason
                )
            )

    async def verify_admission(self, release_id, generation):
        self.schedules.verify(release_id, generation)
        snapshot = self._snapshot
        if snapshot is None or snapshot.release_id != release_id:
            raise Conflict("Managed runtime does not hold the selected release")
        root = staged_release_path(self.workspace, release_id)
        # Verify immutable artifact bytes as well as currently installed code.
        current = await asyncio.to_thread(load_snapshot, root, expected_digest=release_id)
        await asyncio.to_thread(self.assets.verify, current, root)
        if self._snapshot is not snapshot:
            raise Conflict("Runtime selection changed during admission")
        self.schedules.verify(release_id, generation)

    async def _verify_transition(self, record, restoring):
        operation = (str(record["id"]), restoring)
        if self._operation != operation or self._bound != (_fingerprint(record), restoring):
            raise Conflict("Native runtime was not reconciled for this control operation")
        release = record["source_release_id"] if restoring else record["target_release_id"]
        generation = self._generation
        if release is None:
            self.schedules.verify(None, generation)
            if self._snapshot is not None:
                raise Conflict("Managed runtime was not restored to the empty baseline")
        else:
            await self.verify_admission(release, generation)
        if self._operation != operation or self._bound != (_fingerprint(record), restoring):
            raise Conflict("Runtime transition changed during verification")
        return {
            "transition_id": str(record["id"]),
            "release_id": release,
            "runtime_generation": generation,
            "restoring": restoring,
        }

    def verify(self, record, *, restoring=False):
        """Coordinator-only synchronous bridge; never inspect scheduler off-loop."""
        try:
            if asyncio.get_running_loop() is self.loop:
                raise Conflict("Run deployment control through the native async runtime")
        except RuntimeError:
            pass
        if self.loop.is_closed() or not self.loop.is_running():
            raise Conflict("Native engine loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(
            self._verify_transition(record, restoring), self.loop
        )
        try:
            return future.result(timeout=90)
        except BaseException:
            future.cancel()
            raise

    async def commit(self, transition_id, *, actor):
        operator(actor)
        async with self._lock:
            await self._reconcile(transition_id)
            self._operation = (str(transition_id), False)
            try:
                return await _drain_thread(
                    lambda: self.coordinator.commit(transition_id, self, actor=actor)
                )
            finally:
                self._operation = None

    async def abort(self, transition_id, *, actor, reason):
        operator(actor)
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
            raise ValueError("An explicit abort reason is required")
        async with self._lock:
            await self._reconcile(transition_id, restoring=True)
            self._operation = (str(transition_id), True)
            try:
                return await _drain_thread(
                    lambda: self.coordinator.abort(transition_id, self, actor=actor, reason=reason)
                )
            finally:
                self._operation = None

    async def readiness(self):
        if not self._booted:
            raise Conflict("Managed sales startup verification has not completed")
        state = await asyncio.to_thread(self._state)
        if state["pending"] is not None:
            raise Conflict("Sales deployment awaits verified commit or restoration")
        release = state["config"].get("fleet_release_id")
        if release is None:
            self.schedules.verify(None, self._generation)
        else:
            await self.verify_admission(release, self._generation)
        # Refuse a control transition that began while asset checks were running.
        if await asyncio.to_thread(self._state) != state:
            raise Conflict("Sales deployment state changed during readiness verification")
        return "ok"
