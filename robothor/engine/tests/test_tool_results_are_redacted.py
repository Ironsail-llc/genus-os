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


# ── a result is redacted as data, not as a log line ──────────────────────────

PY_SOURCE = (
    "import os\n"
    "api_key = settings.api_key\n"
    "token = get_token()\n"
    'PASSWORD = os.environ["PASSWORD"]\n'
    "secret_key = config.secret_key  # read at startup\n"
    "access_token = parse(x)\n"
    'default_secret = "change-me-in-production"\n'
    "retries = 3\n"
)
YAML_SOURCE = "provider:\n  api_key: ${API_KEY}\n  token: ${TOKEN}\n"
SHELL_SOURCE = 'export TOKEN=$TOKEN\nexport API_KEY="$API_KEY"\nGITHUB_TOKEN=$(gh auth token)\n'


@pytest.mark.parametrize("text", [PY_SOURCE, YAML_SOURCE, SHELL_SOURCE], ids=["py", "yaml", "sh"])
def test_ordinary_code_passes_through_unchanged(text):
    from robothor.secrets.redaction import redact_tool_result

    assert redact_tool_result({"content": text}) == {"content": text}


@pytest.mark.parametrize(
    "line",
    [
        'api_key = "sk-live-' + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF" + '"',
        "TOKEN=ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        'db_password = "Xk92mQ7vLp04Rt8sWz6n"',
    ],
    ids=["sk-live", "ghp", "password"],
)
def test_a_real_credential_in_code_is_redacted(line):
    from robothor.secrets.redaction import redact_tool_result

    cleaned = redact_tool_result({"content": line})["content"]
    value = line.split("=", 1)[1].strip().strip('"')
    leaked = value in cleaned
    assert not leaked, "a credential assigned in code was kept"


def test_read_then_write_round_trips_byte_identical():
    """The model reads the file, edits nothing, writes it back: the bytes it
    writes are exactly the ones it was given, not ``<redacted>``."""
    session = _session()
    session.record_tool_call("read_file", {"path": "app/settings.py"}, {"content": PY_SOURCE}, "c1")
    seen = json.loads(session.messages[-1]["content"])["content"]
    assert seen == PY_SOURCE


# ── a failure on a path that holds a tool result logs its class, not its text ─


def _logged(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_a_failed_symbol_node_logs_no_tool_output(monkeypatch, caplog):
    from robothor.engine import feature_flags, symbolic_memory

    monkeypatch.setattr(feature_flags, "symbolic_memory_mode", lambda: "observe")

    def boom(run_id):
        raise ValueError(f"could not index {SHAPED}")

    monkeypatch.setattr(symbolic_memory, "get_or_create_graph", boom)
    with caplog.at_level("DEBUG", logger="robothor.engine.session"):
        _session()._record_symbol_node("exec", {"stdout": "x"}, 1)
    leaked = SHAPED in _logged(caplog)
    assert not leaked
    assert "ValueError" in _logged(caplog)


def test_a_failed_step_insert_logs_no_row(monkeypatch, caplog):
    from robothor.engine import tracking

    def boom(*a, **k):
        raise RuntimeError(f"Failing row contains ({BARE})")

    monkeypatch.setattr(tracking, "create_steps_batch", boom)
    monkeypatch.setattr(tracking, "create_step", boom)
    session = _session()
    session.record_tool_call("exec", {}, {"stdout": "ok"}, "c1")
    with caplog.at_level("WARNING", logger="robothor.engine.session"):
        session.flush_new_steps_sync()
    leaked = BARE in _logged(caplog)
    assert not leaked


@pytest.mark.parametrize("action", ["save", "load"])
def test_a_failed_checkpoint_logs_no_messages(caplog, action):
    from robothor.engine.checkpoint import CheckpointManager

    def boom(*a, **k):
        raise RuntimeError(f"Failing row contains ({BARE})")

    with (
        patch("robothor.db.connection.get_connection", side_effect=boom),
        caplog.at_level("WARNING", logger="robothor.engine.checkpoint"),
    ):
        if action == "save":
            mgr = CheckpointManager(run_id="test")
            mgr.record_success()
            mgr.save(3, [{"role": "user", "content": "hi"}])
        else:
            CheckpointManager.load_latest("test")
    leaked = BARE in _logged(caplog)
    assert not leaked
    assert "RuntimeError" in _logged(caplog)
