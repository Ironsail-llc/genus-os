"""A tool result passes the secrets redactor before it is stored or replayed.

``exec`` printed a CLI credentials file, and the values went into the model's
context, ``agent_run_steps.tool_output``, ``agent_runtime_effects.resolution``
and the checkpoints. Nothing redacted a tool result that returned normally.
Now every result goes through ``robothor.secrets.redaction.redact`` before any
of those see it. A tool on ``SECRET_RESULT_TOOLS`` hands its result to the
model unchanged, and is still masked wherever it is stored. A call through
``tool_call`` is treated as the tool it reaches.

No assertion here prints a credential: each leak check computes a bool first.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from robothor.secrets.redaction import forget_known_values, register_known_value

#: A shape the redactor recognises.
SHAPED = "ghp_FAKE0000_printed_by_a_cli_credentials_file_0000"
#: No shape at all: only the known-value registry can take it out.
BARE = "5e1f0c9a7b3d2e4f6a8c0b1d3e5f7a9c"


@pytest.fixture(autouse=True)
def _known_bare_value():
    forget_known_values()
    register_known_value(BARE, fingerprint="b2:test")
    yield
    forget_known_values()


def _session():
    from robothor.engine.session import AgentSession

    return AgentSession(agent_id="main", tenant_id="default")


def _credentials_file() -> dict:
    return {"stdout": f"[default]\naccess_token = {SHAPED}\nrefresh = {BARE}\n", "exit_code": 0}


def _leaks(blob) -> bool:
    text = json.dumps(blob, default=str)
    return SHAPED in text or BARE in text


@pytest.fixture
def secret_tool(monkeypatch):
    from robothor.engine import session as session_module

    monkeypatch.setattr(session_module, "SECRET_RESULT_TOOLS", frozenset({"fake_secret_tool"}))
    return "fake_secret_tool"


class TestTheStepAndTheModel:
    def test_an_exec_that_prints_a_credential_reaches_neither(self):
        session = _session()
        step = session.record_tool_call("exec", {"command": "cat creds"}, _credentials_file(), "c1")
        leaked = _leaks(step.tool_output)
        assert not leaked, "tool_output kept the credential"
        leaked = _leaks(session.messages)
        assert not leaked, "the model's context got the credential"
        assert "access_token" in json.dumps(step.tool_output)

    def test_a_tool_on_the_allowlist_reaches_the_model_but_not_the_step(self, secret_tool):
        session = _session()
        step = session.record_tool_call(secret_tool, {}, {"value": SHAPED}, "c1")
        leaked = _leaks(step.tool_output)
        assert not leaked
        reached = SHAPED in json.dumps(session.messages)
        assert reached, "an allowlisted tool's result must reach the model unchanged"

    def test_a_wrapped_call_follows_the_tool_it_reaches(self, secret_tool):
        session = _session()
        step = session.record_tool_call(
            "tool_call", {"name": secret_tool, "arguments": {}}, {"value": SHAPED}, "c1"
        )
        leaked = _leaks(step.tool_output)
        assert not leaked
        reached = SHAPED in json.dumps(session.messages)
        assert reached

    def test_a_wrapped_exec_is_redacted_for_the_model_too(self, secret_tool):
        session = _session()
        step = session.record_tool_call(
            "tool_call", {"name": "exec", "arguments": {}}, _credentials_file(), "c1"
        )
        leaked = _leaks(step.tool_output)
        assert not leaked
        leaked = _leaks(session.messages)
        assert not leaked

    def test_the_tool_still_gets_its_own_result_unchanged(self):
        output = _credentials_file()
        _session().record_tool_call("exec", {}, output, "c1")
        unchanged = SHAPED in output["stdout"]
        assert unchanged, "redaction must not mutate the handler's result"

    def test_an_image_survives(self):
        session = _session()
        b64 = "iVBORw0KGgo" + "A" * 64
        session.record_tool_call("view_image", {}, {"image_base64": b64}, "c1")
        assert b64 in json.dumps(session.messages)


class TestTheLiveStream:
    @pytest.mark.asyncio
    async def test_tool_end_does_not_stream_the_credential(self):
        from robothor.engine.tool_turn import ToolTurnMixin

        events: list = []

        async def on_tool(event):
            events.append(event)

        call = SimpleNamespace(tool_name="exec", tc=SimpleNamespace(id="c1"), elapsed_ms=1)
        await ToolTurnMixin._emit_tool_end(
            call, SimpleNamespace(on_tool=on_tool), _credentials_file(), None
        )
        (end,) = events
        leaked = _leaks(end)
        assert not leaked


class TestTheEffectsRow:
    def test_a_recorded_result_is_redacted(self):
        from robothor.engine.runtime import effect_results

        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.rowcount = 1
        conn.cursor.return_value = cursor
        context = SimpleNamespace(tenant_id="default", principal_id="p", request_id="r")
        with patch.object(effect_results.effects, "get_connection", return_value=conn):
            effect_results.record(context, "e1", "run-1", _credentials_file())
        stored = cursor.execute.call_args.args[1][0]
        leaked = _leaks(getattr(stored, "adapted", stored))
        assert not leaked


class TestTheCheckpoint:
    def test_an_allowlisted_result_is_masked_in_the_checkpoint(self, secret_tool):
        from robothor.engine.checkpoint import CheckpointManager

        session = _session()
        session.record_tool_call(secret_tool, {}, {"value": SHAPED}, "c1")
        mgr = CheckpointManager(run_id="test")
        mgr.record_success()
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        with patch("robothor.db.connection.get_connection", return_value=conn):
            assert mgr.save(3, session.messages) is True
        leaked = _leaks(cursor.execute.call_args.args[1][2])
        assert not leaked
