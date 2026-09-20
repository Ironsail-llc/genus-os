"""Experimental shared-runtime bridge; the injected host owns persistence and tools.

Not selected by production admission. Checkpoint resumption remains unsupported
and is refused before any model call rather than replaying an uncertain action.
"""

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Protocol

from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.runtime.contracts import (
    ProgressEvent,
    RunRequest,
    RuntimeResult,
    RuntimeStoppedError,
    StateEnvelope,
    Usage,
)
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.deadlines import constrain_context, execute_before_deadline


class CandidateHost(Protocol):
    """Trusted product services; none of these operations are model-selected.

    prepare validates the entire context and options, persists runtime identity,
    and supplies the authorized tool gateway. finish durably records the result,
    including unknown usage. control returns only after committing stop authority.
    """

    async def prepare(
        self, request: RunRequest, identity: StateEnvelope
    ) -> tuple[AgentRun, object]: ...

    async def bind_candidate(self, request: RunRequest, candidate): ...

    async def finish(self, result: RuntimeResult) -> None: ...

    async def control(self, tenant: str, run_id: str, action: str, note: str) -> dict: ...


class CandidateRuntime:
    def __init__(self, candidate, host: CandidateHost):
        from bench.runtime.candidates import DeepAgentsCandidate, PydanticCandidate

        if isinstance(candidate, PydanticCandidate):
            name, package = "pydantic-ai", "pydantic-ai-slim"
        elif isinstance(candidate, DeepAgentsCandidate):
            name, package = "deepagents", "deepagents"
        else:
            raise ValueError("unsupported candidate")
        self.identity = StateEnvelope(name, version(package), 1)
        self.candidate, self.host = candidate, host
        self._active = {}

    async def control(self, tenant, run_id, action, note=""):
        if action not in {"pause", "cancel"}:
            raise ValueError("runtime control must be pause or cancel")
        result = await self.host.control(tenant, run_id, action, note)
        # Host commits authority before local interruption; other workers use durable checks.
        task = self._active.get((tenant, run_id))
        if task:
            task.cancel()
        return result

    async def run(self, request, on_event=None):
        if request.resume_from or request.checkpoint:
            raise ValueError("candidate checkpoint resumption is not supported; replay denied")
        context = constrain_context(request.context)
        cap = datetime.now(UTC) + timedelta(seconds=60)
        context = replace(context, deadline=min(context.deadline, cap) if context.deadline else cap)
        request = replace(request, context=context)
        token = active_context.set(context)
        started = time.monotonic()

        async def emit(phase):
            if on_event:
                try:
                    await asyncio.wait_for(
                        on_event(
                            ProgressEvent(context.request_id, phase, time.monotonic() - started)
                        ),
                        timeout=1,
                    )
                except Exception:
                    logging.getLogger(__name__).debug(
                        "Runtime progress delivery failed", exc_info=True
                    )

        try:
            await emit("accepted")
            # Host validates identity/authority and persists runtime identity before returning.
            run, gateway = await execute_before_deadline(
                context, lambda: self.host.prepare(request, self.identity)
            )
            if (run.tenant_id, run.user_id, run.correlation_id, run.parent_run_id) != (
                context.tenant_id,
                context.principal_id,
                context.request_id,
                context.parent_id,
            ):
                raise ValueError("host run identity does not match trusted context")
            return await self._execute(request, run, gateway, started, emit)
        finally:
            active_context.reset(token)

    async def _execute(self, request, run, gateway, started, emit):
        async def progress():
            while True:
                await asyncio.sleep(10)
                await emit("running")

        heartbeat = asyncio.create_task(progress())
        key = (request.context.tenant_id, run.id)
        self._active[key] = asyncio.current_task()
        usage, verified = Usage(None, None, None, None), False
        run.status = RunStatus.RUNNING
        try:
            candidate = await self.host.bind_candidate(request, self.candidate)
            report = await execute_before_deadline(
                request.context,
                lambda: candidate.run(
                    gateway, tenant=request.context.tenant_id, prompt=request.message
                ),
            )
            usage = Usage(
                *(
                    report.get(k)
                    for k in ("model_calls", "input_tokens", "output_tokens", "cost_usd")
                )
            )
            verified = bool(report.get("verified") and gateway.verified)
            run.status = RunStatus.COMPLETED if verified else RunStatus.FAILED
            run.verified_status = "verified" if verified else "failed_verification"
            run.output_text = "Verified completion" if verified else "Completion not verified"
        except (asyncio.CancelledError, RuntimeStoppedError):
            run.status = RunStatus.CANCELLED
            run.error_message = "Execution stopped; reconcile already dispatched effects"
        except Exception as error:
            run.status = RunStatus.TIMEOUT if isinstance(error, TimeoutError) else RunStatus.FAILED
            run.error_message = str(error)
        finally:
            self._active.pop(key, None)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        run.completed_at = datetime.now(UTC)
        run.duration_ms = round((time.monotonic() - started) * 1000)
        for field, value in (
            ("input_tokens", usage.input_tokens),
            ("output_tokens", usage.output_tokens),
            ("total_cost_usd", usage.cost_usd),
        ):
            if value is not None:
                setattr(run, field, value)
        result = RuntimeResult(run, usage, verified, not verified, self.identity)
        await self.host.finish(result)
        await emit(str(run.status))
        return result
