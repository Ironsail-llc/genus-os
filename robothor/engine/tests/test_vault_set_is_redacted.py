"""The one tool whose ARGUMENTS are a credential.

Everything else in this change is about keeping credentials out of places they
should never reach. ``vault_set`` is the deliberate exception — the assistant
is handed a token and stores it — and that makes its argument list the one
place a credential enters the engine by design.

It must not stay there. ``session.record_tool_call`` writes ``tool_input``
into ``agent_run_steps``, which outlives the run, is read by the verification
pass, the guardrail engine and the Helm run viewer, and is exactly the kind of
row that ends up in a support bundle. A redactor that covers logs and tool
OUTPUT and not this would be a redactor with a hole shaped like the one tool
that carries a secret.

The coverage guard at the bottom is the part that matters in a year: it reads
the tool schemas and fails if a NEW tool declares a credential-shaped parameter
nobody taught the redactor about. A hand-maintained list beside the thing it
describes is the drift defect this codebase has shipped three times.
"""

from __future__ import annotations

import json

import pytest

FAKE_TOKEN = "ghp_FAKE0000_must_not_reach_a_stored_row_0000"


def _session():
    from robothor.engine.session import AgentSession

    return AgentSession(agent_id="main", tenant_id="default")


def test_a_vault_set_argument_is_redacted_in_the_stored_step():
    session = _session()
    step = session.record_tool_call(
        tool_name="vault_set",
        tool_input={"key": "providers/github/api_key", "value": FAKE_TOKEN},
        tool_output={"success": True, "key": "providers/github/api_key"},
        tool_call_id="call_1",
    )
    assert FAKE_TOKEN not in json.dumps(step.tool_input, default=str), (
        "the credential was written into agent_run_steps.tool_input"
    )
    assert step.tool_input["key"] == "providers/github/api_key", (
        "redaction must leave the KEY readable — an operator has to be able to "
        "see which row was written"
    )


def test_the_credential_does_not_reach_the_transcript():
    session = _session()
    session.record_tool_call(
        tool_name="vault_set",
        tool_input={"key": "providers/github/api_key", "value": FAKE_TOKEN},
        tool_output={"success": True, "key": "providers/github/api_key"},
        tool_call_id="call_1",
    )
    transcript = json.dumps(session.messages, default=str)
    assert FAKE_TOKEN not in transcript


def test_an_ordinary_tool_argument_is_left_alone():
    """Redacting everything would be safe and useless: the verification pass
    reads ``tool_input`` to decide whether a run did what it claimed."""
    session = _session()
    step = session.record_tool_call(
        tool_name="write_file",
        tool_input={"path": "notes.md", "content": "the quarterly numbers"},
        tool_output={"written": True},
        tool_call_id="call_2",
    )
    assert step.tool_input["content"] == "the quarterly numbers"


def test_a_credential_shaped_argument_name_is_redacted_on_any_tool():
    """Not a list of tool names: any argument CALLED a token is treated as one,
    so a new tool is covered before anybody remembers to cover it."""
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    cleaned = redact_tool_arguments(
        "some_future_tool", {"api_key": FAKE_TOKEN, "endpoint": "https://example.test"}
    )
    assert cleaned["api_key"] == PLACEHOLDER
    assert cleaned["endpoint"] == "https://example.test"


def test_redaction_never_mutates_the_arguments_the_tool_ran_with():
    """The handler has already run by the time this happens, but a redactor
    that edited its input in place would be one refactor away from redacting a
    credential before the vault got it."""
    from robothor.secrets.redaction import redact_tool_arguments

    original = {"key": "providers/github/api_key", "value": FAKE_TOKEN}
    redact_tool_arguments("vault_set", original)
    assert original["value"] == FAKE_TOKEN


def test_every_credential_bearing_tool_parameter_is_covered():
    """The anti-drift guard.

    Reads the engine's own tool schemas and fails if a tool declares a
    parameter that reads as a credential and the redactor does not cover it.
    Adding such a tool therefore fails here, not in a support bundle.
    """
    from robothor.engine.tools.schemas import get_engine_schemas
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    probe = "placeholder-credential-1234567890"
    uncovered: list[str] = []
    for name, schema in get_engine_schemas().items():
        params = (schema.get("function", {}).get("parameters") or {}).get("properties") or {}
        for parameter in params:
            cleaned = redact_tool_arguments(name, {parameter: probe})
            if _reads_as_a_credential(name, parameter) and cleaned[parameter] != PLACEHOLDER:
                uncovered.append(f"{name}.{parameter}")
    assert not uncovered, (
        "these tool parameters carry a credential and are stored unredacted in "
        f"agent_run_steps.tool_input: {uncovered}"
    )


def _reads_as_a_credential(tool: str, parameter: str) -> bool:
    """The test's own opinion of what a credential parameter looks like.

    Deliberately independent of the redactor's: a guard that asks the thing it
    is guarding what to check cannot fail.
    """
    lowered = parameter.lower()
    if any(word in lowered for word in ("token", "secret", "password", "api_key", "apikey")):
        return True
    return tool.startswith("vault_") and lowered == "value"


@pytest.mark.parametrize("parameter", ["value", "token", "api_key", "password", "secret"])
def test_the_guard_itself_recognises_the_shapes_it_claims_to(parameter):
    assert _reads_as_a_credential("vault_set", parameter)


# ── the other stores a value could reach ─────────────────────────────────────


def test_an_audit_row_cannot_carry_a_credential():
    """An audit row outlives the run and is exported into a support bundle.

    Tool ARGUMENTS were never written here — the row carries a tool name, an
    actor and a tenant. The one field that could carry a value is ``error``,
    which is sometimes the text of an exception somebody else raised, which is
    the exact shape the redactor exists for.
    """
    from robothor.engine.tools import dispatch

    recorded: list[dict] = []

    class _Logger:
        @staticmethod
        def log_event(**kwargs):
            recorded.append(kwargs)

    import sys
    import types

    module = types.ModuleType("robothor.audit.logger")
    module.log_event = _Logger.log_event
    original = sys.modules.get("robothor.audit.logger")
    sys.modules["robothor.audit.logger"] = module
    try:
        dispatch._audit_tool_call(
            "vault_set",
            "main",
            "default",
            status="error",
            error=f"upstream refused: Bearer {FAKE_TOKEN}",
        )
    finally:
        if original is not None:
            sys.modules["robothor.audit.logger"] = original
        else:
            del sys.modules["robothor.audit.logger"]

    assert recorded, "the audit call recorded nothing"
    assert FAKE_TOKEN not in json.dumps(recorded, default=str)


# ── the live tool stream ─────────────────────────────────────────────────────


def _turn_request(events: list):
    from types import SimpleNamespace

    async def on_tool(event):
        events.append(event)

    run = SimpleNamespace(
        id="r",
        tenant_id="default",
        user_id="u",
        user_role="owner",
        accessible_tenant_ids=(),
        is_benchmark=False,
    )
    return SimpleNamespace(
        on_tool=on_tool,
        trace=None,
        agent_config=SimpleNamespace(id="main", tool_timeout_seconds=120, task_author_override=""),
        session=SimpleNamespace(run=run, identity=None),
    )


@pytest.mark.asyncio
async def test_the_tool_start_event_does_not_stream_the_credential():
    """Every stored path redacted a ``vault_set`` argument; the live one did
    not. ``tool_start`` carried the raw arguments onto the webchat SSE stream
    and into the Telegram progress handler, so the credential reached the
    browser in clear while every row about the call said ``<redacted>``."""
    from types import SimpleNamespace

    from robothor.engine.tool_turn import ToolTurnMixin

    events: list = []

    async def execute(name, args, **kwargs):
        return {"success": True}

    executor = ToolTurnMixin.__new__(ToolTurnMixin)
    executor.registry = SimpleNamespace(execute=execute)  # type: ignore[attr-defined]
    executor.config = SimpleNamespace(workspace="/tmp")  # type: ignore[attr-defined]
    args = {"key": "providers/github/api_key", "value": FAKE_TOKEN}
    call = SimpleNamespace(
        tool_name="vault_set",
        tool_args=args,
        tc=SimpleNamespace(id="c1"),
        result=None,
        elapsed_ms=0,
    )
    await executor._execute_one(call, _turn_request(events))  # type: ignore[arg-type]

    (start,) = [e for e in events if e.get("event") == "tool_start"]
    leaked = FAKE_TOKEN in json.dumps(start, default=str)
    assert not leaked, "tool_start streamed the credential"
    assert start["args"]["key"] == "providers/github/api_key"
    assert call.tool_args["value"] == FAKE_TOKEN, "the handler must still get the real value"


# ── a credential reached through a wrapper ───────────────────────────────────
#
# On a deferred run the model does not call ``vault_set``; it calls
# ``tool_call {name: vault_set, arguments: {key, value}}``. The tool NAME every
# redaction site sees is then ``tool_call``, and the value sits one level down.
# Redaction has to follow the call to the tool it actually reaches.

#: Not credential-shaped on purpose: 32 hex characters, the form an API key
#: usually takes, which no shape pattern can tell from a digest. Only the
#: argument-level rule can catch it.
PLAIN_HEX_VALUE = "0123456789abcdef0123456789abcdef"


def _wrapped_vault_set(value: str = PLAIN_HEX_VALUE) -> dict:
    return {
        "name": "vault_set",
        "arguments": {"key": "providers/weather/api_key", "value": value},
    }


def test_a_vault_set_reached_through_tool_call_is_redacted():
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    cleaned = redact_tool_arguments("tool_call", _wrapped_vault_set())
    assert PLAIN_HEX_VALUE not in json.dumps(cleaned)
    assert cleaned["name"] == "vault_set"
    assert cleaned["arguments"]["key"] == "providers/weather/api_key"
    assert cleaned["arguments"]["value"] == PLACEHOLDER


def test_a_doubly_wrapped_vault_set_is_redacted():
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    outer = {"name": "tool_call", "arguments": _wrapped_vault_set()}
    cleaned = redact_tool_arguments("tool_call", outer)
    assert PLAIN_HEX_VALUE not in json.dumps(cleaned)
    assert cleaned["arguments"]["arguments"]["value"] == PLACEHOLDER
    assert cleaned["arguments"]["arguments"]["key"] == "providers/weather/api_key"


def test_a_wrapped_call_whose_arguments_are_a_json_string_is_redacted():
    """Models send ``arguments`` as a JSON string often enough that the
    wrapper's own handler is not the only reader that has to cope."""
    from robothor.secrets.redaction import redact_tool_arguments

    wrapped = {"name": "vault_set", "arguments": json.dumps(_wrapped_vault_set()["arguments"])}
    cleaned = redact_tool_arguments("tool_call", wrapped)
    assert PLAIN_HEX_VALUE not in json.dumps(cleaned)


def test_a_credential_named_argument_nested_in_a_wrapper_is_redacted():
    """``mcp_call_tool`` forwards an ``arguments`` object to an external tool;
    a nested ``api_key`` is the same credential it is at the top level."""
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    cleaned = redact_tool_arguments(
        "mcp_call_tool",
        {"server_name": "s", "tool_name": "t", "arguments": {"api_key": PLAIN_HEX_VALUE}},
    )
    assert cleaned["arguments"]["api_key"] == PLACEHOLDER
    assert cleaned["tool_name"] == "t"


def test_a_known_value_inside_a_wrapper_is_redacted():
    """Known-value redaction applies to the wrapper's arguments too: a value
    the vault already holds, pasted into an ordinary nested argument."""
    from robothor.secrets.redaction import (
        forget_known_values,
        redact_tool_arguments,
        register_known_value,
    )

    register_known_value(PLAIN_HEX_VALUE, fingerprint="abcd")
    try:
        cleaned = redact_tool_arguments(
            "tool_call",
            {
                "name": "write_file",
                "arguments": {"path": "notes.md", "content": f"key is {PLAIN_HEX_VALUE}"},
            },
        )
    finally:
        forget_known_values()
    assert PLAIN_HEX_VALUE not in json.dumps(cleaned)
    assert cleaned["arguments"]["path"] == "notes.md"


def test_wrapper_redaction_never_mutates_the_arguments_the_tool_ran_with():
    from robothor.secrets.redaction import redact_tool_arguments

    original = {"name": "tool_call", "arguments": _wrapped_vault_set()}
    redact_tool_arguments("tool_call", original)
    assert original["arguments"]["arguments"]["value"] == PLAIN_HEX_VALUE


def test_a_tool_call_wrapped_vault_set_is_redacted_in_the_stored_step():
    session = _session()
    step = session.record_tool_call(
        tool_name="tool_call",
        tool_input=_wrapped_vault_set(),
        tool_output={"success": True, "key": "providers/weather/api_key"},
        tool_call_id="call_w1",
    )
    assert PLAIN_HEX_VALUE not in json.dumps(step.tool_input, default=str), (
        "the credential was written into agent_run_steps.tool_input"
    )
    assert PLAIN_HEX_VALUE not in json.dumps(session.messages, default=str)
    assert step.tool_input["arguments"]["key"] == "providers/weather/api_key"


def test_a_doubly_wrapped_vault_set_is_redacted_in_the_stored_step():
    session = _session()
    step = session.record_tool_call(
        tool_name="tool_call",
        tool_input={"name": "tool_call", "arguments": _wrapped_vault_set()},
        tool_output={"error": "tool_call cannot invoke itself or another meta-tool"},
        tool_call_id="call_w2",
    )
    assert PLAIN_HEX_VALUE not in json.dumps(step.tool_input, default=str)


def test_the_assistant_echo_of_a_wrapped_vault_set_is_redacted():
    """The provider's reply carries the call as a JSON string; that message is
    replayed to the provider and recorded, so it is a store like the step."""
    from robothor.secrets.redaction import redact_assistant_turn

    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "tool_call", "arguments": json.dumps(_wrapped_vault_set())},
            }
        ],
    }
    cleaned = redact_assistant_turn(message)
    assert PLAIN_HEX_VALUE not in json.dumps(cleaned)
    assert message["tool_calls"][0]["function"]["arguments"].count(PLAIN_HEX_VALUE) == 1


async def _tool_start_for(tool_name: str, tool_args: dict) -> dict:
    from types import SimpleNamespace

    from robothor.engine.tool_turn import ToolTurnMixin

    events: list = []

    async def execute(name, args, **kwargs):
        return {"success": True}

    executor = ToolTurnMixin.__new__(ToolTurnMixin)
    executor.registry = SimpleNamespace(execute=execute)  # type: ignore[attr-defined]
    executor.config = SimpleNamespace(workspace="/tmp")  # type: ignore[attr-defined]
    call = SimpleNamespace(
        tool_name=tool_name,
        tool_args=tool_args,
        tc=SimpleNamespace(id="c1"),
        result=None,
        elapsed_ms=0,
    )
    await executor._execute_one(call, _turn_request(events))  # type: ignore[arg-type]
    (start,) = [e for e in events if e.get("event") == "tool_start"]
    return start


@pytest.mark.asyncio
async def test_the_tool_start_event_does_not_stream_a_tool_call_wrapped_credential():
    args = _wrapped_vault_set()
    start = await _tool_start_for("tool_call", args)
    assert PLAIN_HEX_VALUE not in json.dumps(start, default=str), (
        "tool_start streamed the credential"
    )
    assert start["tool"] == "tool_call"
    assert start["args"]["arguments"]["key"] == "providers/weather/api_key"
    assert args["arguments"]["value"] == PLAIN_HEX_VALUE, (
        "the handler must still get the real value"
    )


@pytest.mark.asyncio
async def test_the_tool_start_event_does_not_stream_a_doubly_wrapped_credential():
    start = await _tool_start_for(
        "tool_call", {"name": "tool_call", "arguments": _wrapped_vault_set()}
    )
    assert PLAIN_HEX_VALUE not in json.dumps(start, default=str)


def test_a_workflow_step_stores_a_wrapped_vault_set_redacted():
    from types import SimpleNamespace

    from robothor.engine.workflow import _stored_tool_input

    stored = _stored_tool_input(
        SimpleNamespace(tool_name="tool_call", tool_input=_wrapped_vault_set())  # type: ignore[arg-type]
    )
    assert stored is not None
    assert PLAIN_HEX_VALUE not in stored


@pytest.mark.parametrize("padded", [" vault_set", "vault_set ", "\tvault_set\n"])
def test_a_padded_wrapped_tool_name_is_redacted_as_the_tool_it_reaches(padded):
    """``tool_call`` strips the name before it dispatches, so a padded name
    still runs ``vault_set``; the redactor has to read it the same way."""
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    wrapped = {"name": padded, "arguments": _wrapped_vault_set()["arguments"]}
    cleaned = redact_tool_arguments("tool_call", wrapped)
    assert cleaned["arguments"]["value"] == PLACEHOLDER


def test_a_json_string_wrapper_past_the_depth_cap_is_the_placeholder():
    """A dict wrapper past the cap becomes the placeholder; a JSON-string
    wrapper at the same depth has to as well, or the cap is a hole: shape-only
    redaction cannot see a plain hex credential the argument rule would catch."""
    from robothor.secrets import redaction
    from robothor.secrets.redaction import PLACEHOLDER, redact_tool_arguments

    innermost = {"name": "vault_set", "arguments": json.dumps({"value": PLAIN_HEX_VALUE})}
    wrapped = innermost
    # The vault_set wrapper lands exactly at the cap, so its JSON-string
    # arguments are the first thing past it.
    for _ in range(redaction._MAX_ARGUMENT_DEPTH):
        wrapped = {"name": "tool_call", "arguments": wrapped}
    cleaned = redact_tool_arguments("tool_call", wrapped)
    assert PLAIN_HEX_VALUE not in json.dumps(cleaned)
    assert PLACEHOLDER in json.dumps(cleaned)
