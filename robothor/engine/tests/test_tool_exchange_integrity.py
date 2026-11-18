"""A tool exchange is never split: every result keeps its call, whatever sits between.

Measured on a live instance: a run's first tool batch tripped the
sensitive-data guardrail, whose one-time engine notice was appended BEFORE the
tool results were recorded, so the history read

    system, user, assistant(tool_calls), developer notice, tool[, tool]

On the next iteration compaction treated the notice as the end of the
exchange, dropped the assistant tool-call message alone and kept its results
("Compaction complete: 6 → 5 messages", tokens unchanged). The client's
pairing check returned early because no tool call was left, the orphaned
results reached the provider, the primary failed and the fallback rejected the
request ("No tool call found for function call output"), and the run ended
with every model failed.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine.compaction import (
    _find_safe_split_index,
    protected_prefix_len,
)
from robothor.engine.context import maybe_compress
from robothor.engine.context_control import drain_history
from robothor.engine.llm_client import LLMClient
from robothor.engine.session import ENGINE_CONTEXT_ROLE

PRIMARY = "vertex_ai/example-flash"
FALLBACK = "openai/example-model"
CALL_A = "call_1001"
CALL_B = "call_1002"


def _report(chars: int) -> str:
    row = {"control": "access_review", "status": "ok", "detail": "x" * 200}
    return json.dumps({"rows": [row] * (chars // 260)})


def _history(two_calls: bool) -> list[dict]:
    calls = [{"id": CALL_A, "type": "function", "function": {"name": "report", "arguments": "{}"}}]
    if two_calls:
        calls.append(
            {"id": CALL_B, "type": "function", "function": {"name": "report", "arguments": "{}"}}
        )
    messages = [
        {"role": "system", "content": "You are a monitor."},
        {"role": "user", "content": "Run the sweep and report."},
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": ENGINE_CONTEXT_ROLE, "content": "[SYSTEM] Possible credential in report output."},
        {"role": "tool", "tool_call_id": CALL_A, "content": _report(300_000)},
    ]
    if two_calls:
        messages.append({"role": "tool", "tool_call_id": CALL_B, "content": _report(300_000)})
    return messages


def _orphans(messages: list[dict]) -> list[str]:
    """Results with no earlier call, and calls with no result: both are rejected."""
    called: set[str] = set()
    answered: set[str] = set()
    orphans = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            called.add(call["id"])
        if m.get("role") == "tool":
            answered.add(m["tool_call_id"])
            if m["tool_call_id"] not in called:
                orphans.append(m["tool_call_id"])
    return orphans + sorted(called - answered)


async def _no_network(*_a, **_k):
    raise RuntimeError("no network in tests")


@pytest.mark.parametrize("two_calls", [True, False], ids=["two-calls", "one-call"])
class TestTheProductionShape:
    async def test_compaction_never_orphans_a_result(self, two_calls):
        with patch("robothor.engine.compaction._acompletion_over_chain", new=_no_network):
            out = await maybe_compress(_history(two_calls), [PRIMARY, FALLBACK], threshold=80_000)
        assert _orphans(out) == []

    async def test_the_request_sent_to_the_fallback_is_paired(self, two_calls):
        from litellm.completion_extras.litellm_responses_transformation.transformation import (
            LiteLLMResponsesTransformationHandler,
        )

        with patch("robothor.engine.compaction._acompletion_over_chain", new=_no_network):
            out = await maybe_compress(_history(two_calls), [PRIMARY, FALLBACK], threshold=80_000)
        sent = LLMClient._build_llm_kwargs(FALLBACK, out, [], 1000, 0.2)["messages"]
        assert _orphans(sent) == []
        items, _ = (
            LiteLLMResponsesTransformationHandler().convert_chat_completion_messages_to_responses_api(
                sent
            )
        )
        seen: set[str] = set()
        for item in map(dict, items):
            if item.get("type") == "function_call":
                seen.add(item["call_id"])
            elif item.get("type") == "function_call_output":
                assert item["call_id"] in seen, "a function_call_output with no call"

    def test_drain_removes_whole_exchanges(self, two_calls):
        out = drain_history(_history(two_calls), PRIMARY, target=1_000)
        assert _orphans(out) == []


class TestTheExchangeBoundary:
    def test_a_split_never_lands_between_a_call_and_its_results(self):
        messages = _history(True)
        assert _find_safe_split_index(messages, len(messages) - 1) == 2

    def test_the_protected_head_does_not_end_inside_an_exchange(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": ENGINE_CONTEXT_ROLE, "content": "note"},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
        ]
        assert protected_prefix_len(messages, 3) == 1


class TestThePairingCheckIsTheLastLine:
    def test_orphaned_results_are_dropped_with_no_call_left(self, caplog):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": ENGINE_CONTEXT_ROLE, "content": "note"},
            {"role": "tool", "tool_call_id": CALL_A, "content": "r"},
            {"role": "tool", "tool_call_id": CALL_B, "content": "r"},
        ]
        with caplog.at_level(logging.WARNING, logger="robothor.engine.llm_client"):
            out = LLMClient._validate_tool_pairs(messages)
        assert [m["role"] for m in out] == ["system", "user", ENGINE_CONTEXT_ROLE]
        assert any("2" in r.getMessage() and "orphan" in r.getMessage() for r in caplog.records)


class TestTheGuardrailNoticeComesAfterTheResults:
    def test_the_notice_is_queued_not_appended_mid_batch(self):
        from robothor.engine.post_execution import (
            SENSITIVE_DATA_GUARDRAIL,
            apply_post_execution_guardrails,
            drain_engine_notes,
        )

        session = SimpleNamespace(messages=[], run=SimpleNamespace(id="", steps=[]))
        verdict = SimpleNamespace(
            action="warned", guardrail_name=SENSITIVE_DATA_GUARDRAIL, reason="Possible key."
        )
        engine = SimpleNamespace(check_post_execution=lambda *_a: verdict)
        apply_post_execution_guardrails(
            session,
            engine,
            tool_name="read_file",
            result={"content": "token"},
            error_msg=None,
            state=SimpleNamespace(secret_notified=False),
        )
        assert session.messages == [], "the notice landed between a call and its result"
        notes = drain_engine_notes(session)
        assert len(notes) == 1 and "Possible key." in notes[0]
        assert drain_engine_notes(session) == []


class TestProviderErrorsAreLoggedWithTheirText:
    @pytest.mark.parametrize("status", [401, 402, 403, 500, 503])
    def test_the_provider_message_is_in_the_log(self, status, caplog):
        error = RuntimeError("No tool call found for function call output with call_id call_1")
        error.status_code = status  # type: ignore[attr-defined]
        with caplog.at_level(logging.WARNING, logger="robothor.engine.llm_client"):
            LLMClient._handle_model_error(error, "openai/example-model", set())
        text = " ".join(r.getMessage() for r in caplog.records)
        assert str(status) in text
        assert "No tool call found" in text

    def test_a_credential_in_the_error_is_redacted(self, caplog):
        error = RuntimeError("bad key sk-live-abcdefghijklmnopqrstuvwxyz0123456789")
        error.status_code = 401  # type: ignore[attr-defined]
        with caplog.at_level(logging.WARNING, logger="robothor.engine.llm_client"):
            LLMClient._handle_model_error(error, "openai/example-model", set())
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in " ".join(
            r.getMessage() for r in caplog.records
        )


class TestEveryNoteWaitsForTheBatch:
    """Two other engine notes were appended per call, so a note could land
    between two results of one assistant message: the same split, and the
    Anthropic leg rewrites the note as a user turn that the provider rejects."""

    def test_the_repeated_failure_note_is_queued(self):
        from robothor.engine.post_execution import drain_engine_notes
        from robothor.engine.tool_outcome import CIRCUIT_BREAKER_THRESHOLD, _count_failure

        session = SimpleNamespace(messages=[])
        failures: dict[str, int] = {}
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            _count_failure(session, "web_fetch", "boom", failures)
        assert session.messages == []
        notes = drain_engine_notes(session)
        assert len(notes) == 1 and "web_fetch" in notes[0]

    def test_the_checklist_nudge_is_queued(self):
        from robothor.engine.post_execution import drain_engine_notes
        from robothor.engine.tool_turn import ToolTurnMixin

        session = SimpleNamespace(messages=[], todo_list=object())
        call = SimpleNamespace(tool_name="todo_write")
        req = SimpleNamespace(session=session)
        ToolTurnMixin._after_todo_write(call, req, {"verificationNudgeNeeded": True}, None)
        assert session.messages == []
        assert any("verify your work" in note for note in drain_engine_notes(session))
