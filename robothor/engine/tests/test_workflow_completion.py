"""A trusted workflow result ends only its owning native run after tools finish."""

import asyncio

import pytest

from robothor.engine.session import AgentSession


def test_completion_records_workflow_origin_and_failure_stays_failure():
    from robothor.engine.workflow_completion import (
        WorkflowCompletion,
        WorkflowCompletionError,
        finish_after_tools,
        workflow_completion_scope,
    )

    session = AgentSession("parent", tenant_id="tenant-a")
    result = None
    with workflow_completion_scope("tenant-a", "parent", lambda: result):
        assert not finish_after_tools(session)
        result = WorkflowCompletion(output='{"result":"validated"}')
        assert finish_after_tools(session)
        assert session.get_final_text() == result.output
        assert session.run.steps[-1].tool_output["origin"] == "trusted_workflow"
        assert not finish_after_tools(session)  # consume only once
    with workflow_completion_scope(
        "tenant-a", "parent", lambda: WorkflowCompletion(error="Incomplete bundle")
    ):
        with pytest.raises(WorkflowCompletionError, match="Incomplete bundle"):
            finish_after_tools(session)


async def test_completion_is_identity_scoped_and_closed_for_detached_tasks():
    from robothor.engine.workflow_completion import (
        WorkflowCompletion,
        finish_after_tools,
        workflow_completion_scope,
    )

    release = asyncio.Event()

    async def detached():
        await release.wait()
        return finish_after_tools(AgentSession("parent", tenant_id="tenant-a"))

    with workflow_completion_scope("tenant-a", "parent", lambda: WorkflowCompletion(output="ok")):
        assert not finish_after_tools(AgentSession("child", tenant_id="tenant-a"))
        assert not finish_after_tools(AgentSession("parent", tenant_id="other"))
        task = asyncio.create_task(detached())
    release.set()
    assert not await task


def test_workflow_completion_does_not_bypass_final_output_validation():
    from robothor.engine.output_validation import output_validation_scope, validated_completion
    from robothor.engine.workflow_completion import (
        WorkflowCompletion,
        finish_after_tools,
        workflow_completion_scope,
    )

    session = AgentSession("parent", tenant_id="tenant-a")
    with (
        workflow_completion_scope("tenant-a", "parent", lambda: WorkflowCompletion(output="bad")),
        output_validation_scope(lambda run, text: "Unacceptable result"),
    ):
        assert finish_after_tools(session)
        assert validated_completion(session, session.get_final_text()).status == "failed"
