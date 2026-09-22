"""A committed effect with a missing response needs observation, not a retry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.error_actions import apply_error_recovery
from robothor.engine.tests.test_tool_outcome import _record, _session


@pytest.mark.parametrize(
    "message", ["bridge service timed out", "backing service error (HTTP 500)"]
)
@pytest.mark.parametrize("consecutive", [1, 3])
async def test_unknown_effect_overrides_timeout_retry_and_helper_recovery(message, consecutive):
    session = _session()
    with patch("robothor.engine.tracking.log_tool_event"):
        category = _record(
            session,
            tool_name="create_note",
            error_msg=message,
            result={"error": message, "retryable": False, "outcome_unknown": True},
        )
    helper = AsyncMock()
    result = await apply_error_recovery(
        session,
        SimpleNamespace(can_spawn_agents=True),
        iteration_errors=[("create_note", message, category)],
        escalation=SimpleNamespace(consecutive_errors=consecutive),
        readonly_mode=False,
        helper_spawns_used=0,
        spawn_helper=helper,
    )
    helper.assert_not_awaited()
    assert result.applied
    text = "\n".join(item["content"] for item in session.messages)
    assert "read-only" in text and "audit" in text
    assert "Do not repeat" in text
    assert "retrying" not in text.lower()
