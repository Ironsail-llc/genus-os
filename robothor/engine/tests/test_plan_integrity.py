from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from robothor.engine.chat import _extract_plan_text
from robothor.engine.plan_integrity import require_alignment
from robothor.engine.session import AgentSession


def response(text):
    return SimpleNamespace(
        model="test", usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


async def test_irrelevant_plan_retries_once_then_has_no_approval():
    session = AgentSession("main")
    session.start("system", "Deploy the browser repair", [])
    session.record_llm_call = MagicMock()
    runner = SimpleNamespace(_call_llm=AsyncMock(return_value=response("MISMATCH")))
    assert await require_alignment(runner, session, ["test"], "Merge CRM contacts")
    assert not await require_alignment(runner, session, ["test"], "Merge CRM contacts")
    assert _extract_plan_text(session.messages[-1]["content"]) == ""


async def test_relevant_plan_and_clarification_can_pass():
    session = AgentSession("main")
    session.start("system", "Get this live", [])
    session.record_llm_call = MagicMock()
    runner = SimpleNamespace(_call_llm=AsyncMock(return_value=response("ALIGNED")))
    assert not await require_alignment(runner, session, ["test"], "Deploy browser repair")
    session.record_llm_call.assert_called_once()


async def test_unavailable_verifier_does_not_approve():
    session = AgentSession("main")
    session.start("system", "Deploy repair", [])
    runner = SimpleNamespace(_call_llm=AsyncMock(side_effect=TimeoutError))
    assert await require_alignment(runner, session, ["test"], "Plan")
    assert not await require_alignment(runner, session, ["test"], "Plan")
    assert _extract_plan_text(session.messages[-1]["content"]) == ""
