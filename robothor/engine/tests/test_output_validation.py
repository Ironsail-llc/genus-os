"""Workflow output repair stays inside the existing run and fails closed."""

import asyncio

import pytest

from robothor.engine.session import AgentSession


def session(text="bad"):
    value = AgentSession("worker")
    value.messages = [{"role": "assistant", "content": text}]
    return value


def test_invalid_output_gets_bounded_feedback_and_never_completes_at_budget_exit():
    from robothor.engine.output_validation import (
        OutputValidationError,
        output_validation_scope,
        request_output_repair,
        validated_completion,
    )

    run = session()
    with output_validation_scope(
        lambda run, text: None if text == "valid" else "Use the result contract", max_repairs=1
    ):
        assert request_output_repair(run)
        assert run.messages[-1]["role"] == "developer"
        assert "Use the result contract" in run.messages[-1]["content"]
        run.messages.append({"role": "assistant", "content": "still bad"})
        with pytest.raises(OutputValidationError):
            request_output_repair(run)
        assert validated_completion(run, "bad").status == "failed"
        run.messages.append({"role": "assistant", "content": "valid"})
        assert not request_output_repair(run)
        assert validated_completion(run, "valid").status == "completed"


async def test_validation_does_not_leak_between_concurrent_or_detached_runs():
    from robothor.engine.output_validation import output_validation_scope, request_output_repair

    release = asyncio.Event()

    async def inherited():
        await release.wait()
        return request_output_repair(session())

    async def scoped():
        with output_validation_scope(lambda run, text: "Invalid"):
            task = asyncio.create_task(inherited())
            await asyncio.sleep(0)
            assert request_output_repair(session())
        release.set()
        assert not await task

    async def ordinary():
        await asyncio.sleep(0)
        assert not request_output_repair(session())

    await asyncio.gather(scoped(), ordinary())
