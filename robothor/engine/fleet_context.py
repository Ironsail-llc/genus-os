"""In-process admission context established only by managed native callbacks."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from inspect import isawaitable

from robothor.operations.store import Conflict


@dataclass(frozen=True)
class FleetInvocation:
    tenant: str
    release_id: str
    workflow_id: str
    verify_current: Callable[[], object] = field(repr=False)


invocation: ContextVar[FleetInvocation | None] = ContextVar("fleet_invocation", default=None)


async def assert_current(tenant, release_id, workflow_id):
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
