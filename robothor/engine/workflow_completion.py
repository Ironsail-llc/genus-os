"""Trusted workflow decisions after a native tool turn; never model-authored flags."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from robothor.engine.models import RunStep, StepType


class WorkflowCompletionError(RuntimeError):
    """A completed workflow operation failed its required result contract."""


@dataclass(frozen=True)
class WorkflowCompletion:
    output: str = ""
    error: str = ""


@dataclass
class _Completion:
    tenant_id: str
    agent_id: str
    resolve: Callable[[], WorkflowCompletion | None]
    active: bool = True
    consumed: bool = False


_completion: ContextVar[_Completion | None] = ContextVar("workflow_completion", default=None)


@contextmanager
def workflow_completion_scope(tenant_id, agent_id, resolve):
    state = _Completion(tenant_id, agent_id, resolve)
    token = _completion.set(state)
    try:
        yield
    finally:
        state.active = False
        _completion.reset(token)


def finish_after_tools(session):
    """Resolve only after tools finish; the runner retains normal final validation."""
    state = _completion.get()
    if (
        state is None
        or not state.active
        or state.consumed
        or session.run.tenant_id != state.tenant_id
        or session.run.agent_id != state.agent_id
    ):
        return False
    result = state.resolve()
    if result is None:
        return False
    if not isinstance(result, WorkflowCompletion):
        raise WorkflowCompletionError("Invalid trusted workflow completion")
    state.consumed = True
    if result.error:
        raise WorkflowCompletionError(result.error)
    # A checkpoint explicitly distinguishes workflow-authored final content
    # from an LLM call. No model usage or delivery is invented here.
    now = datetime.now(UTC)
    session._step_counter += 1
    session.run.steps.append(
        RunStep(
            run_id=session.run.id,
            step_number=session._step_counter,
            step_type=StepType.CHECKPOINT,
            tool_name="workflow_completion",
            tool_output={"origin": "trusted_workflow", "output": result.output},
            started_at=now,
            completed_at=now,
            duration_ms=0,
        )
    )
    session.messages.append({"role": "assistant", "content": result.output})
    return True
