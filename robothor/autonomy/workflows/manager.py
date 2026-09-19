"""Broker-owned browser lifetimes; controllers receive only references and metadata."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan, url_origin
from robothor.autonomy.inspection import inspect_page
from robothor.autonomy.workflows.store import WorkflowStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from playwright.async_api import Browser, Page, Route, StorageState

    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore


@dataclass
class LiveWorkflow:
    scope: Scope
    agent_id: str
    browser: Browser
    page: Page
    broker: BrowserBroker
    opened_at: float
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WorkflowManager:
    def __init__(
        self,
        store: AutonomyStore,
        browser_factory: Callable[[], Awaitable[Browser]],
        *,
        request_router: Callable[[Route], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from robothor.autonomy.worker import public_request

        self.store = store
        self.repository = WorkflowStore(store)
        self.instance_id = str(uuid4())
        self.browser_factory = browser_factory
        self.request_router = request_router or public_request
        self.clock = clock
        self._live: dict[str, LiveWorkflow] = {}
        self._opening = asyncio.Lock()
        self.accepting = True

    def drain(self) -> None:
        self.accepting = False

    def resume_admission(self) -> None:
        self.accepting = True

    @property
    def opening(self) -> bool:
        return self._opening.locked()

    @property
    def active_count(self) -> int:
        return len(self._live)

    def _expired(self, live: LiveWorkflow) -> bool:
        now = self.clock()
        return now - live.last_used >= 900 or now - live.opened_at >= 3600

    async def _discard(self, workflow_id: str, live: LiveWorkflow, state: str = "lost") -> None:
        # The caller holds the page lock. Closing the browser is unconditional,
        # including when the durable journal is temporarily unavailable.
        try:
            await asyncio.to_thread(
                self.repository.close, live.scope, live.agent_id, workflow_id, state
            )
        finally:
            self._live.pop(workflow_id, None)
            await live.browser.close()

    async def expire_idle(self) -> None:
        for workflow_id, live in list(self._live.items()):
            if live.lock.locked() or not self._expired(live):
                continue
            async with live.lock:
                if self._live.get(workflow_id) is live and self._expired(live):
                    await self._discard(workflow_id, live)

    async def status(self, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        async with self._opening:
            return await self._status(scope, agent_id, workflow_id)

    async def _status(self, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self.repository.get, scope, agent_id, workflow_id)
        if row["state"] == "open" and (
            row["instance_id"] != self.instance_id or workflow_id not in self._live
        ):
            await asyncio.to_thread(self.repository.close, scope, agent_id, workflow_id, "lost")
            row["state"] = "lost"
        operation = await asyncio.to_thread(self.store.operation, scope, row["operation_id"])
        return {
            "workflow_id": workflow_id,
            "operation_id": row["operation_id"],
            "state": row["state"],
            "revision": row["revision"],
            "operation_state": operation["state"],
        }

    async def close(self, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        async with self._opening:
            await asyncio.to_thread(self.repository.get, scope, agent_id, workflow_id)
            live = self._live.get(workflow_id)
            if live:
                async with live.lock:
                    await self._discard(workflow_id, live)
            return await self._status(scope, agent_id, workflow_id)

    async def open(
        self,
        scope: Scope,
        agent_id: str,
        operation_id: str,
        url: str,
        *,
        session_resource_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._opening:
            if not self.accepting:
                return {"error": "workflow_broker_draining"}
            operation = await asyncio.to_thread(self.store.operation, scope, operation_id)
            if url_origin(url) != operation["proposal"]["origin"]:
                raise PermissionError("destination_mismatch")
            grant = await asyncio.to_thread(
                self.store.check_authority, scope, operation_id, agent_id
            )
            request = {"url": url, "session_resource_id": session_resource_id}
            row = await asyncio.to_thread(
                self.repository.open, scope, agent_id, operation_id, self.instance_id, request
            )
            workflow_id = row["id"]
            if row["instance_id"] != self.instance_id:
                await asyncio.to_thread(self.repository.close, scope, agent_id, workflow_id, "lost")
                raise PermissionError("workflow_lost")
            if row["state"] != "open":
                raise PermissionError("workflow_not_open")
            if workflow_id not in self._live:
                if len(self._live) >= 16:
                    await asyncio.to_thread(
                        self.repository.close, scope, agent_id, workflow_id, "closed"
                    )
                    raise PermissionError("workflow_capacity_reached")
                browser = None
                try:
                    storage = None
                    if session_resource_id:
                        storage = await asyncio.to_thread(
                            self.store.consume_resource,
                            scope,
                            session_resource_id,
                            row["origin"],
                            kind="browser_session",
                        )
                    browser = await self.browser_factory()
                    context = await browser.new_context(
                        storage_state=cast("StorageState | None", storage),
                        accept_downloads=False,
                        service_workers="block",
                    )
                    await context.route("**/*", self.request_router)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if url_origin(page.url) != row["origin"]:
                        raise PermissionError("destination_changed")
                    self._live[workflow_id] = LiveWorkflow(
                        scope,
                        agent_id,
                        browser,
                        page,
                        BrowserBroker(self.store),
                        self.clock(),
                        self.clock(),
                    )
                except BaseException:
                    if browser:
                        await browser.close()
                    await asyncio.to_thread(
                        self.repository.close, scope, agent_id, workflow_id, "lost"
                    )
                    raise
            live = self._live[workflow_id]
            async with live.lock:
                row = await asyncio.to_thread(self.repository.get, scope, agent_id, workflow_id)
                if row["state"] != "open":
                    raise PermissionError("workflow_not_open")
                if self._expired(live):
                    await self._discard(workflow_id, live)
                    raise PermissionError("workflow_lost")
                inspection = await inspect_page(
                    live.page, destination=row["origin"], allowed_frames=grant.frame_origins
                )
                live.last_used = self.clock()
            return {
                "workflow_id": workflow_id,
                "operation_id": operation_id,
                "state": "open",
                "revision": row["revision"],
                **inspection,
            }

    async def inspect(self, scope: Scope, agent_id: str, workflow_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self.repository.get, scope, agent_id, workflow_id)
        live = self._live.get(workflow_id)
        if row["instance_id"] != self.instance_id or live is None:
            raise PermissionError("workflow_lost")
        async with live.lock:
            row = await asyncio.to_thread(self.repository.get, scope, agent_id, workflow_id)
            if self._live.get(workflow_id) is not live or self._expired(live):
                if self._live.get(workflow_id) is live:
                    await self._discard(workflow_id, live)
                raise PermissionError("workflow_lost")
            grant = await asyncio.to_thread(
                self.store.check_authority, scope, row["operation_id"], agent_id
            )
            inspection = await inspect_page(
                live.page, destination=row["origin"], allowed_frames=grant.frame_origins
            )
            live.last_used = self.clock()
            return {
                "workflow_id": workflow_id,
                "revision": row["revision"],
                **inspection,
            }

    async def execute(
        self,
        scope: Scope,
        agent_id: str,
        workflow_id: str,
        command_id: str,
        revision: int,
        plan: ExecutionPlan,
        *,
        advance: bool = False,
        verification_code: str | None = None,
    ) -> dict[str, Any]:
        row = await asyncio.to_thread(self.repository.get, scope, agent_id, workflow_id)
        request = {
            "kind": "advance" if advance else "submit",
            "revision": revision,
            "plan": plan.model_dump(mode="json"),
        }
        live = self._live.get(workflow_id)
        lock = live.lock if live else asyncio.Lock()
        async with lock:
            if live and self._expired(live):
                await self._discard(workflow_id, live)
                live = None
            prior = await asyncio.to_thread(
                self.repository.begin,
                scope,
                agent_id,
                workflow_id,
                self.instance_id,
                command_id,
                request,
            )
            if prior is not None:
                return prior
            if live is None:
                await asyncio.to_thread(self.repository.close, scope, agent_id, workflow_id, "lost")
                return {
                    "state": "reconciling",
                    "reason": "workflow_lost",
                    "workflow_id": workflow_id,
                }
            try:
                grant = await asyncio.to_thread(
                    self.store.check_authority, scope, row["operation_id"], agent_id
                )
                result = await live.broker.execute_on_page(
                    scope,
                    row["operation_id"],
                    agent_id,
                    plan,
                    live.page,
                    allowed_frames=grant.frame_origins,
                    verification_code=verification_code,
                    workflow_id=workflow_id,
                    navigate=False,
                    advance=advance,
                )
                changed = (
                    result.get("reason")
                    in {"workflow_step_completed", "server_validation_required"}
                    or result.get("state") == "completed"
                )
                result = {**result, "workflow_id": workflow_id, "revision": revision + int(changed)}
                await asyncio.to_thread(
                    self.repository.finish,
                    scope,
                    agent_id,
                    workflow_id,
                    command_id,
                    result,
                    advance=changed,
                )
                live.last_used = self.clock()
                if result.get("state") in {"completed", "reconciling"}:
                    await self._discard(
                        workflow_id, live, "completed" if result["state"] == "completed" else "lost"
                    )
                return result
            except BaseException:
                if self._live.get(workflow_id) is live:
                    await self._discard(workflow_id, live)
                raise

    async def shutdown(self) -> None:
        for workflow_id, live in list(self._live.items()):
            async with live.lock:
                try:
                    await self._discard(workflow_id, live)
                except Exception:
                    # One journal failure must not leave other browsers alive.
                    continue
