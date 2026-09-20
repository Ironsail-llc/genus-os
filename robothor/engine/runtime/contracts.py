"""Execution contracts. Product services own authority, business state and delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from robothor.engine.models import AgentRun


@dataclass(frozen=True)
class ExecutionContext:
    tenant_id: str
    principal_id: str
    request_id: str
    parent_id: str | None = None
    goal_id: str | None = None
    attempt_id: str | None = None
    budget_id: str | None = None
    deadline: datetime | None = None
    parent_goal_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.principal_id, self.request_id)):
            raise ValueError("tenant, principal and request identity are required")
        if bool(self.goal_id) != bool(self.attempt_id):
            raise ValueError("goal and attempt identity must travel together")
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("deadline must include a timezone")


@dataclass(frozen=True)
class StateEnvelope:
    runtime_id: str = "current"
    runtime_version: str = "1"
    checkpoint_version: int = 1

    def require_compatible(self, other: StateEnvelope) -> None:
        if self != other:
            raise ValueError("incompatible runtime checkpoint; explicit migration required")


@dataclass(frozen=True)
class RunRequest:
    context: ExecutionContext
    agent_id: str
    message: str
    # A trusted host supplies native manifest/identity objects. Never model-generated kwargs.
    options: dict[str, Any] = field(default_factory=dict)
    resume_from: str | None = None
    checkpoint: StateEnvelope | None = None


@dataclass(frozen=True)
class ProgressEvent:
    request_id: str
    phase: str
    elapsed_s: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    model_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class RuntimeResult:
    run: AgentRun
    usage: Usage
    # Engine completion does not establish the business objective's truth.
    verified: bool
    unresolved: bool
    runtime: StateEnvelope = field(default_factory=StateEnvelope)


class AgentRuntime(Protocol):
    """Host expiry raises RuntimeDeadlineError; native cancellation records remain authoritative."""

    identity: StateEnvelope

    async def run(
        self,
        request: RunRequest,
        on_event: Callable[[ProgressEvent], Awaitable[None]] | None = None,
    ) -> RuntimeResult: ...

    async def control(
        self, tenant: str, run_id: str, action: str, note: str = ""
    ) -> dict[str, Any]: ...
