"""In-process admission context established only by managed native callbacks."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from inspect import isawaitable

from robothor.operations.store import Conflict


@dataclass(frozen=True)
class FleetInvocation:
    tenant: str
    # None is the unmanaged baseline: `assert_current` treats a missing release
    # and a missing context together, and the scheduler registers jobs with
    # whatever `reconcile` resolved, which is None for an empty snapshot.
    release_id: str | None
    workflow_id: str
    verify_current: Callable[[], object] = field(repr=False)


invocation: ContextVar[FleetInvocation | None] = ContextVar("fleet_invocation", default=None)


async def assert_current(tenant: str, release_id: str | None, workflow_id: str) -> None:
    context = invocation.get()
    if release_id is None and context is None:
        return  # Existing unmanaged workflows retain their native behavior.
    if context is None or (context.tenant, context.release_id, context.workflow_id) != (
        tenant,
        release_id,
        workflow_id,
    ):
        raise Conflict("Sales queue requires its selected native fleet generation")
    verified = context.verify_current()
    if isawaitable(verified):
        await verified
