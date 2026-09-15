"""The pasted credential must not outlive the turn that stored it.

Review finding C1, which the first cut's own test certified as fixed while
being vacuous. ``record_tool_call`` redacted ``tool_input`` — but the value
does not enter the engine through ``tool_input``. It enters twice:

1. **The operator's message**, pasted into Telegram or webchat. It has to reach
   the model once — that is the whole feature — but after that turn it is
   appended to ``ChatSession.history``, persisted to Postgres, and replayed to
   the provider on every subsequent turn of the session, for as long as the
   session lives.
2. **The assistant's own echo**: ``tool_calls[].function.arguments`` on the
   assistant message. ``record_llm_call`` appended that to ``session.messages``
   verbatim, so it was (a) re-sent to the provider every following turn, (b)
   written into the LLM_CALL step's ``tool_output`` under
   ``ROBOTHOR_RECORD_ASSISTANT_TURNS``, and (c) serialised into the ShareGPT
   trajectory JSONL when sampling is on.

The old test asserted on a transcript that had never contained the value,
because it never called ``record_llm_call``. These record the turn the way the
runner does.

The rule this file pins: **the current turn may see it; nothing after may.**
Redacting before the model's first sight would break the feature — the
assistant could not store a token it cannot read.
"""

from __future__ import annotations

import json

import pytest

TOKEN = "ghp_FAKE0000_pasted_by_the_operator_0000"


def _session():
    from robothor.engine.session import AgentSession

    return AgentSession(agent_id="main", tenant_id="default")


def _assistant_turn_storing_the_token() -> dict:
    """What the provider sends back when the model calls ``vault_set``."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "vault_set",
                    "arguments": json.dumps({"key": "providers/github/api_key", "value": TOKEN}),
                },
            }
        ],
    }


# ── the assistant's echo ─────────────────────────────────────────────────────


def test_the_assistant_turn_does_not_carry_the_value_into_the_transcript():
    """``session.messages`` is re-sent to the provider on every later turn."""
    session = _session()
    session.record_llm_call(
        model="test-model", assistant_message=_assistant_turn_storing_the_token()
    )
    assert TOKEN not in json.dumps(session.messages, default=str), (
        "the credential is in the conversation and will be re-sent to the model "
        "provider on every subsequent turn of this session"
    )


def test_the_recorded_assistant_turn_does_not_carry_the_value_into_a_step(monkeypatch):
    """With turn recording on, the turn lands in ``agent_run_steps``."""
    monkeypatch.setenv("ROBOTHOR_RECORD_ASSISTANT_TURNS", "true")
    session = _session()
    step = session.record_llm_call(
        model="test-model", assistant_message=_assistant_turn_storing_the_token()
    )
    assert TOKEN not in json.dumps(step.tool_output, default=str)


def test_the_tool_name_and_the_key_survive_redaction():
    """Redacting the whole turn would be safe and useless: the verification
    pass and the run viewer read this to say what the agent did."""
    session = _session()
    session.record_llm_call(
        model="test-model", assistant_message=_assistant_turn_storing_the_token()
    )
    transcript = json.dumps(session.messages, default=str)
    assert "vault_set" in transcript
    assert "providers/github/api_key" in transcript


def test_an_ordinary_tool_call_is_untouched():
    session = _session()
    session.record_llm_call(
        model="test-model",
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "notes.md", "content": "quarterly"}),
                    },
                }
            ],
        },
    )
    assert "quarterly" in json.dumps(session.messages, default=str)


def test_the_trajectory_export_does_not_carry_the_value():
    """The ShareGPT JSONL is written to disk for fine-tuning corpora."""
    from robothor.engine.trajectory import _to_sharegpt

    rows = _to_sharegpt([_assistant_turn_storing_the_token()])
    assert TOKEN not in json.dumps(rows, default=str)


# ── the operator's own message ───────────────────────────────────────────────


def test_the_pasted_message_is_redacted_before_it_enters_the_session_history():
    """The current turn saw it. Every turn after this must not.

    ``ChatSession.history`` is BOTH the replay source handed to
    ``conversation_history=`` and the thing ``save_session`` persists, so one
    redaction at append closes both.
    """
    from robothor.engine.chat import ChatSession, append_turn

    session = ChatSession()
    append_turn(session, user_message=f"here is the token: {TOKEN}", assistant_text="Stored.")
    assert TOKEN not in json.dumps(session.history, default=str)
    assert "here is the token" in json.dumps(session.history, default=str), (
        "redaction must take the value, not the message — an operator reading "
        "their own history back needs to see what they said"
    )


def test_replayed_history_is_redacted_on_the_way_to_the_model():
    """Belt to the braces above: a history row written by an earlier release,
    or by a path that bypassed the append helper, must not reach the provider.
    """
    from robothor.engine.session import _render_history_for_llm

    rendered = _render_history_for_llm({"role": "user", "content": f"token: {TOKEN}"})
    assert TOKEN not in json.dumps(rendered, default=str)


def test_the_current_turn_still_sees_the_value():
    """The feature. An assistant that cannot read the token cannot store it —
    a redactor applied one step too early is a broken product, not a safe one.
    """
    session = _session()
    session.start(
        system_prompt="you are main",
        user_message=f"store this: {TOKEN}",
        tools_provided=[],
    )
    assert TOKEN in json.dumps(session.messages, default=str)


@pytest.mark.parametrize(
    "shape",
    [
        "ghp_FAKE1111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "xoxb-FAKE-2222-2222-fakefakefakefake",
        "sk-FAKE3333aaaaaaaaaaaaaaaaaaaaaa",
        "github_pat_FAKE4444aaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_every_vendor_shape_the_operator_might_paste_is_taken(shape):
    from robothor.engine.chat import ChatSession, append_turn

    session = ChatSession()
    append_turn(session, user_message=f"the new key is {shape}", assistant_text="ok")
    assert shape not in json.dumps(session.history, default=str)
