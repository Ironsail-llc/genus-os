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
