"""Setup caps cannot be extended by late classification or delegated work."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from robothor.engine.runtime.classification_window import (
    ClassificationDeadlineError,
    guard,
    release,
)
from robothor.engine.runtime.deadlines import require_time
from robothor.engine.tests.test_classified_action_deadline import request


@pytest.mark.parametrize("suppress", [False, True])
async def test_unclassified_stall_cannot_return_success(monkeypatch, suppress):
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.02)
    with pytest.raises(ClassificationDeadlineError):
        async with guard(request()):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not suppress:
                    raise


async def test_late_classification_cannot_release_an_expired_limit(monkeypatch):
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.02)
    with pytest.raises(ClassificationDeadlineError):
        async with guard(request()):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                release()


async def test_identified_complex_work_can_continue_past_setup_limit(monkeypatch):
    from robothor.engine.runtime.classified_deadline import admission, apply

    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.02)
    req = request()
    with admission(req):
        async with guard(req):
            await apply(
                None,
                None,
                SimpleNamespace(difficulty_class=""),
                None,
                SimpleNamespace(success=True, difficulty="complex", raw={"difficulty": "complex"}),
            )
            await asyncio.sleep(0.04)
            require_time()


async def test_child_cannot_release_parent_limit_or_dispatch_after_expiry(monkeypatch):
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.02)
    req = request()
    child = replace(req, context=replace(req.context, parent_id="parent"))
    with pytest.raises(ClassificationDeadlineError):
        async with guard(req):
            async with guard(child):
                release()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    require_time()


async def test_expired_admission_does_not_get_another_window():
    with pytest.raises(ClassificationDeadlineError):
        async with guard(request(), datetime.now(UTC) - timedelta(seconds=61)):
            pytest.fail("Execution admitted after deadline")


async def test_unrelated_provider_timeout_keeps_its_cause():
    error = TimeoutError("provider-specific")
    with pytest.raises(TimeoutError) as result:
        async with guard(request()):
            raise error
    assert result.value is error
