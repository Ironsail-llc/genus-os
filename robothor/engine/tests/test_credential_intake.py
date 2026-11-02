"""The owner hands the chat agent a credential; the agent keeps it.

The vault tools were open only to an agent whose own manifest declares
``v2.credentials: operator``. The default chat agent declares no tier, so an
owner who pasted an API key into chat was told to edit a manifest or to "ask
the operator's own agent" — while talking to it. The human's role was never
consulted.

Credential intake is one more way through the gate, and only one: an owner or
admin, typing to the default chat agent in a top-level human chat run.
Everything else stays refused, and the refusal names the gate that held so the
agent can tell the person what would open it. There is no switch: every test
here runs with no intake variable in the environment and with a stale one left
at ``off`` or ``observe``, and the behaviour is the same.

No assertion in this file prints a credential: every leak check computes a
bool first, so a failure reports ``assert not True`` and never the value.
"""

from __future__ import annotations

import pytest

from robothor.engine.models import TriggerType
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.vault import HANDLERS

#: Shapeless on purpose: 32 lower-case hex characters, the form a weather or
#: maps API key takes. No prefix the shape-based redactor could recognise, so
#: only the known-value redaction can take it out of a stored row.
SHAPELESS_KEY = "c0ffee42" * 4
KEY = "providers/openweather/api_key"


#: A retired switch left in an environment must change nothing.
STALE_SWITCH = "ROBOTHOR_CREDENTIAL_INTAKE"


@pytest.fixture(params=[None, "off", "observe"], ids=["unset", "stale-off", "stale-observe"])
def intake(monkeypatch, request):
    """No manifest tier, a vault double, and a live-session registry cleanup."""
    from robothor import secrets as secrets_module
    from robothor import vault
    from robothor.engine import session_registry
    from robothor.engine.tools.handlers import vault as vault_tools
    from robothor.secrets import redaction

    monkeypatch.setattr(vault_tools, "credential_tier", lambda agent_id: "")
    if request.param is None:
        monkeypatch.delenv(STALE_SWITCH, raising=False)
    else:
        monkeypatch.setenv(STALE_SWITCH, request.param)
    monkeypatch.delenv("ROBOTHOR_DEFAULT_CHAT_AGENT", raising=False)

    rows: dict[str, str] = {}
    writes: list[dict] = []

    def fake_set(key, value, **kw):
        rows[key] = value
        writes.append({"key": key, **kw})

    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(vault, "set", fake_set)
    monkeypatch.setattr(vault, "list", lambda **kw: sorted(rows))
    monkeypatch.setattr(vault, "delete", lambda key, **kw: rows.pop(key, None) is not None)

    async def _no_reload() -> None:
        return None

    monkeypatch.setattr(vault_tools, "_reload_cached_readers", _no_reload)
    updates: list[dict] = []
    from robothor.engine import tracking

    monkeypatch.setattr(
        tracking,
        "update_run",
        lambda run_id, **kw: updates.append({"run_id": run_id, **kw}) or True,
    )
    secrets_module.reset_vault_availability()
    registered: list = []

    def live(
        *,
        trigger: TriggerType = TriggerType.WEBCHAT,
        agent_id: str = "main",
        parent_run_id: str | None = None,
        task_text: str | None = None,
    ):
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id=agent_id, trigger_type=trigger, tenant_id="default")
        session.run.parent_run_id = parent_run_id
        session.run.nesting_depth = 1 if parent_run_id else 0
        if task_text is not None:
            session.run.task_text = task_text
            session.originating_message = task_text
        session_registry.register(session)
        registered.append(session)
        return session

    yield {"rows": rows, "writes": writes, "updates": updates, "live": live}

    for session in registered:
        session_registry.unregister(session)
    redaction.forget_known_values()
    secrets_module.reset_vault_availability()


def _ctx(session=None, *, role: str = "owner", agent_id: str = "main") -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        run_id=session.run.id if session is not None else "",
        tenant_id="default",
        user_id="user-alice",
        user_role=role,
    )


def _store_args() -> dict:
    return {"key": KEY, "value": SHAPELESS_KEY}


# ── there is no switch ───────────────────────────────────────────────────────


def test_there_is_no_intake_mode_reader():
    import inspect

    from robothor.engine import feature_flags

    assert not hasattr(feature_flags, "credential_intake_mode")
    assert STALE_SWITCH not in inspect.getsource(feature_flags)


def test_the_retired_switch_is_not_a_declared_setting():
    from robothor.settings.registry import field_index

    declared = {record["env"] for record in field_index().values()}
    assert STALE_SWITCH not in declared


def test_the_handler_says_it_takes_a_credential_in_chat():
    """Capability copy reads a public marker, and the gate it names stays put."""
    from robothor.engine.tools.handlers import vault as vault_tools

    assert vault_tools.CREDENTIAL_INTAKE is True
    assert callable(vault_tools._intake_gate)


# ── the owner's hand-over works ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("trigger", [TriggerType.WEBCHAT, TriggerType.TELEGRAM])
async def test_an_owner_or_admin_can_store_a_credential_in_chat(intake, role, trigger):
    session = intake["live"](trigger=trigger)
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session, role=role))
    assert result.get("success") is True, result.get("error")
    assert result["fingerprint"].startswith("b2:")
    assert "OPENWEATHER_API_KEY" in result["readable_as"]
    assert intake["rows"][KEY] == SHAPELESS_KEY
    leaked = SHAPELESS_KEY in repr(result)
    assert not leaked, "the tool result carried the credential"


@pytest.mark.asyncio
async def test_intake_opens_the_other_vault_tools_too(intake, monkeypatch):
    session = intake["live"]()
    intake["rows"][KEY] = SHAPELESS_KEY
    got = await HANDLERS["vault_get"]({"key": KEY}, _ctx(session))
    assert "error" not in got
    assert got["configured"] is True


@pytest.mark.asyncio
async def test_the_stored_row_records_who_stored_it_and_how(intake, monkeypatch):
    session = intake["live"](trigger=TriggerType.TELEGRAM)
    await HANDLERS["vault_set"](_store_args(), _ctx(session))
    (write,) = intake["writes"]
    meta = write["metadata"]
    assert meta["stored_by"] == "user-alice"
    assert meta["via"] == "chat:telegram"
    assert meta["run_id"] == session.run.id
    assert meta["agent_id"] == "main"
    leaked = SHAPELESS_KEY[:8] in repr(meta)
    assert not leaked, "the audit metadata carried part of the credential"


@pytest.mark.asyncio
async def test_the_default_chat_agent_is_read_from_the_setting(intake, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_DEFAULT_CHAT_AGENT", "concierge")
    session = intake["live"](agent_id="concierge")
    allowed = await HANDLERS["vault_set"](_store_args(), _ctx(session, agent_id="concierge"))
    assert allowed.get("success") is True, allowed.get("error")


# ── everybody else stays refused, and is told why ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "viewer", "user", "service", ""])
async def test_intake_refuses_a_caller_who_is_not_an_owner_or_admin(intake, monkeypatch, role):
    session = intake["live"]()
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session, role=role))
    assert result["denied_by"] == "credential_tier"
    assert result["gate"] == "role"
    assert "owner or admin" in result["error"]
    assert intake["writes"] == []


@pytest.mark.asyncio
async def test_intake_refuses_an_agent_that_is_not_the_default_chat_agent(intake, monkeypatch):
    session = intake["live"](agent_id="researcher")
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session, agent_id="researcher"))
    assert result["denied_by"] == "credential_tier"
    assert result["gate"] == "chat_agent"
    assert intake["writes"] == []


@pytest.mark.asyncio
async def test_intake_refuses_a_sub_agent_of_the_chat_agent(intake, monkeypatch):
    """A child of main runs as main's id only if someone spawns main itself;
    either way a run with a parent is not the owner typing."""
    session = intake["live"](trigger=TriggerType.SUB_AGENT, parent_run_id="parent-run")
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session))
    assert result["denied_by"] == "credential_tier"
    assert result["gate"] == "human_chat"
    assert intake["writes"] == []


@pytest.mark.asyncio
async def test_intake_refuses_a_parented_run_even_on_a_chat_trigger(intake, monkeypatch):
    session = intake["live"](trigger=TriggerType.WEBCHAT, parent_run_id="parent-run")
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session))
    assert result["gate"] == "human_chat"
    assert intake["writes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW, TriggerType.CHANNEL_EVENT],
)
async def test_intake_refuses_an_unattended_run_of_the_chat_agent(intake, monkeypatch, trigger):
    session = intake["live"](trigger=trigger)
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session))
    assert result["gate"] == "human_chat"
    assert intake["writes"] == []


@pytest.mark.asyncio
async def test_intake_refuses_a_manual_trigger(intake, monkeypatch):
    """``MANUAL`` is the CLI, but also the API trigger and resume endpoints and
    the unattended repair-resume of the chat agent, which runs as ``owner``.
    None of those is a person handing over a credential in chat."""
    session = intake["live"](trigger=TriggerType.MANUAL)
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session))
    assert result.get("gate") == "human_chat"
    assert intake["writes"] == []


@pytest.mark.asyncio
async def test_intake_refuses_a_call_with_no_live_run(intake, monkeypatch):
    """No session to read the trigger from is not evidence of a person."""
    result = await HANDLERS["vault_set"](_store_args(), _ctx(None))
    assert result["gate"] == "human_chat"
    assert intake["writes"] == []


@pytest.mark.asyncio
async def test_the_refusal_names_the_unlock(intake, monkeypatch):
    session = intake["live"]()
    result = await HANDLERS["vault_set"](_store_args(), _ctx(session, role="member"))
    assert result["unlock"], "a gated refusal must say what would open it"


# ── the manifest tier still works on its own ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_operator_tier_needs_no_human_in_the_loop(intake, monkeypatch):
    from robothor.engine.tools.handlers import vault as vault_tools

    monkeypatch.setattr(vault_tools, "credential_tier", lambda agent_id: "operator")
    result = await HANDLERS["vault_set"](_store_args(), _ctx(None, role=""))
    assert result.get("success") is True


# ── the value leaves the places the owner's message already reached ──────────


@pytest.mark.asyncio
async def test_the_live_runs_task_text_loses_the_value(intake, monkeypatch):
    """``agent_runs.task_text`` is written at run START, before the agent has
    stored anything, so a shapeless key in the owner's message is already in
    the row by the time ``vault_set`` runs. The handler takes it back out."""
    message = f"here is my weather key {SHAPELESS_KEY} please store it"
    session = intake["live"](task_text=message)
    await HANDLERS["vault_set"](_store_args(), _ctx(session))

    leaked_live = SHAPELESS_KEY in (session.run.task_text or "")
    assert not leaked_live, "the live run's task_text still carries the value"
    assert "<redacted:b2:" in session.run.task_text
    leaked_origin = SHAPELESS_KEY in (session.originating_message or "")
    assert not leaked_origin

    (update,) = [u for u in intake["updates"] if u["run_id"] == session.run.id]
    leaked_row = SHAPELESS_KEY in (update["task_text"] or "")
    assert not leaked_row, "the agent_runs row was not rewritten"


@pytest.mark.asyncio
async def test_after_a_store_every_later_redaction_takes_the_value(intake, monkeypatch):
    """ChatHistory, ``chat_messages`` and the logs all go through ``redact``;
    the owner's turn is appended to them AFTER the run, when the value is
    known."""
    from robothor.engine.chat_history import ChatHistory
    from robothor.secrets.redaction import redact

    session = intake["live"]()
    stored = await HANDLERS["vault_set"](_store_args(), _ctx(session))

    line = f"my key is {SHAPELESS_KEY}"
    cleaned = redact(line)
    leaked = SHAPELESS_KEY in cleaned
    assert not leaked
    assert f"<redacted:{stored['fingerprint']}>" in cleaned

    history = ChatHistory()
    history.append({"role": "user", "content": line})
    leaked_history = SHAPELESS_KEY in repr(list(history))
    assert not leaked_history


# ── the gate does not block the event loop ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool", ["vault_get", "vault_set", "vault_test", "vault_list", "vault_delete"]
)
async def test_the_gate_runs_off_the_event_loop(intake, monkeypatch, tool):
    """The gate reads the agent's manifest from disk: blocking I/O that, on the
    loop thread, stalls every other run for as long as the read takes."""
    import threading

    from robothor.engine.tools.handlers import vault as vault_tools

    seen: list[bool] = []

    def tier(agent_id):
        seen.append(threading.current_thread() is threading.main_thread())
        return ""

    monkeypatch.setattr(vault_tools, "credential_tier", tier)
    session = intake["live"]()
    await HANDLERS[tool](_store_args(), _ctx(session))
    assert seen, "the gate never ran"
    assert not any(seen), "the gate ran on the event-loop thread"
