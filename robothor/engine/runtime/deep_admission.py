"""Check durable controls on the worker thread before starting deep execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.models import AgentRun


def execute_deep_checked(*, run: AgentRun, workspace: str, **kwargs: Any) -> dict[str, Any]:
    from robothor.engine.rlm_tool import DeepReasonConfig, execute_deep_reason
    from robothor.engine.runtime.controls import stopped
    from robothor.engine.runtime.provider_budget import DurableStopError

    if stopped(run.tenant_id, run.id):
        raise DurableStopError("Durable stop denies deep execution")
    from robothor.engine.runtime.deep_tools import owner

    token = owner.set((run.tenant_id, run.id))
    try:
        return execute_deep_reason(config=DeepReasonConfig(workspace=workspace), **kwargs)
    finally:
        owner.reset(token)


def record_deep(run: AgentRun, create_run: Callable[..., Any]) -> str | None:
    import logging

    try:
        create_run(run)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Deep admission audit failed (%s)", type(error).__name__
        )
        return "Deep execution could not be recorded; no work was started."
    return None
