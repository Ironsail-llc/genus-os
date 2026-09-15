"""What the assistant can do with a credential, and what it can never see.

The operator handed the assistant a GitHub token over Telegram. Three things
had to become true for that to be a reasonable thing to do:

* the assistant can KEEP it — ``vault_set``, which already existed,
* the assistant can PROVE it — ``vault_test``, which did not: the only way to
  show a token worked was to use it and report the result, and the only way to
  show WHICH token was stored was to print it,
* the assistant never SEES it — and ``vault_get`` returned the raw value to the
  model, which put every credential the instance owns one tool call away from
  a transcript, a context window and whatever the model said next.

So ``vault_get`` no longer returns a value. It returns the same write-only
shape the Helm Secrets page uses: configured, a fingerprint, where the
accessor would resolve it from, and when the row was last written. The
fingerprint is what lets the assistant answer "which token is stored?" without
anybody reading a credential.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.vault import HANDLERS

#: Obviously fake. Distinctive enough that a substring search over a whole
#: result dict cannot match it by accident.
FAKE_TOKEN = "ghp_FAKE0000_never_returned_to_a_model_0000"


@pytest.fixture(autouse=True)
def _operator_agent(monkeypatch):
    """The three vault tools are operator-tier; most tests run as one."""
    import robothor.engine.tools.handlers.vault as vault_tools

    monkeypatch.setattr(vault_tools, "manifest_role", lambda agent_id, workspace="": "main")


@pytest.fixture
def stored(monkeypatch):
    """A vault holding one row, with no database anywhere near it."""
    from robothor import secrets as secrets_module
    from robothor import vault

    rows: dict[str, str] = {"providers/github/api_key": FAKE_TOKEN}
    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(vault, "export_env", lambda **kw: {"GITHUB_TOKEN": FAKE_TOKEN})
    monkeypatch.setattr(vault, "set", lambda key, value, **kw: rows.__setitem__(key, value))
    monkeypatch.setattr(vault, "list", lambda **kw: sorted(rows))
    secrets_module.reset_vault_availability()
    yield rows
    secrets_module.reset_vault_availability()


def _ctx(agent_id: str = "main") -> ToolContext:
    return ToolContext(agent_id=agent_id, tenant_id="default")


def _flatten(value) -> str:
    return repr(value)


# ── vault_get never returns a value ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_vault_get_returns_a_fingerprint_not_a_value(stored):
    result = await HANDLERS["vault_get"]({"key": "providers/github/api_key"}, _ctx())
    assert FAKE_TOKEN not in _flatten(result), "vault_get handed the model a credential"
    assert result["configured"] is True
    assert result["fingerprint"].startswith("sha256:")
    assert "value" not in result


@pytest.mark.asyncio
async def test_the_fingerprint_identifies_the_row_without_revealing_it(stored):
    """Which token is stored? -- answerable, and only by whoever holds the
    same token to fingerprint."""
    from robothor.secrets.fingerprint import fingerprint

    result = await HANDLERS["vault_get"]({"key": "providers/github/api_key"}, _ctx())
    assert result["fingerprint"] == fingerprint(FAKE_TOKEN)
    assert result["fingerprint"] != fingerprint(FAKE_TOKEN + "x")


@pytest.mark.asyncio
async def test_vault_get_says_where_the_accessor_would_resolve_it_from(stored):
    result = await HANDLERS["vault_get"]({"key": "providers/github/api_key"}, _ctx())
    assert result["source"] in {"env", "vault", "missing", "unavailable"}


@pytest.mark.asyncio
async def test_an_absent_row_is_reported_not_errored(stored):
    """``configured: False`` is an answer. An error would make "is this set?"
    indistinguishable from "did the call fail?"."""
    result = await HANDLERS["vault_get"]({"key": "providers/nothing/api_key"}, _ctx())
    assert result["configured"] is False
    assert result["fingerprint"] is None


# ── vault_test proves it without printing it ─────────────────────────────────


@pytest.mark.asyncio
async def test_vault_test_reports_an_identity_hint_and_no_value(stored, monkeypatch):
    import robothor.secrets.testers as testers

    async def fake_probe(kind, value):
        assert value == FAKE_TOKEN, "the tester must dial with the stored credential"
        return testers.TestOutcome(ok=True, identity_hint="octocat", error_class=None)

    monkeypatch.setattr(testers, "probe", fake_probe)
    result = await HANDLERS["vault_test"]({"key": "providers/github/api_key"}, _ctx())
    assert result["ok"] is True
    assert result["identity_hint"] == "octocat"
    assert FAKE_TOKEN not in _flatten(result)


@pytest.mark.asyncio
async def test_a_dead_credential_is_reported_by_class_not_by_message(stored, monkeypatch):
    import robothor.secrets.testers as testers

    async def fake_probe(kind, value):
        return testers.TestOutcome(ok=False, identity_hint=None, error_class="auth")

    monkeypatch.setattr(testers, "probe", fake_probe)
    result = await HANDLERS["vault_test"]({"key": "providers/github/api_key"}, _ctx())
    assert result["ok"] is False
    assert result["error_class"] == "auth"
    assert FAKE_TOKEN not in _flatten(result)


@pytest.mark.asyncio
async def test_a_key_with_no_known_tester_says_so(stored, monkeypatch):
    result = await HANDLERS["vault_test"]({"key": "providers/nothing/api_key"}, _ctx())
    assert result["ok"] is False
    assert result["error_class"] in {"unknown_kind", "not_configured"}
    assert FAKE_TOKEN not in _flatten(result)


# ── the operator tier ────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["vault_get", "vault_set", "vault_test", "vault_delete"])
@pytest.mark.asyncio
async def test_a_service_role_agent_is_refused(stored, monkeypatch, tool):
    """A spawned sub-agent runs under its OWN agent id, and its own manifest
    declares no operator role -- so this is the sub-agent refusal, stated as
    the rule that produces it."""
    import robothor.engine.tools.handlers.vault as vault_tools

    monkeypatch.setattr(vault_tools, "manifest_role", lambda agent_id, workspace="": "service")
    args = {"key": "providers/github/api_key", "value": "ghp_FAKE9999"}
    result = await HANDLERS[tool](args, _ctx(agent_id="worker"))
    assert "error" in result, f"{tool} was not refused for a service-role agent"
    assert "operator" in result["error"].lower()


@pytest.mark.asyncio
async def test_a_sub_agent_cannot_write_the_vault(stored, monkeypatch):
    """The specific case, run rather than reasoned about."""
    import robothor.engine.tools.handlers.vault as vault_tools

    roles = {"main": "main", "researcher": "service"}
    monkeypatch.setattr(
        vault_tools, "manifest_role", lambda agent_id, workspace="": roles.get(agent_id, "service")
    )
    refused = await HANDLERS["vault_set"](
        {"key": "providers/github/api_key", "value": "ghp_FAKE9999"}, _ctx(agent_id="researcher")
    )
    assert "error" in refused
    assert stored["providers/github/api_key"] == FAKE_TOKEN, "a sub-agent wrote the vault"


@pytest.mark.asyncio
async def test_an_unreadable_manifest_fails_closed(stored, monkeypatch):
    import robothor.engine.tools.handlers.vault as vault_tools

    def boom(agent_id, workspace=""):
        raise RuntimeError("manifest directory is gone")

    monkeypatch.setattr(vault_tools, "manifest_role", boom)
    result = await HANDLERS["vault_set"](
        {"key": "providers/github/api_key", "value": "ghp_FAKE9999"}, _ctx()
    )
    assert "error" in result


# ── a write takes effect at once ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_write_reloads_the_cached_readers(stored, monkeypatch):
    """A rotation that needs a restart is not a rotation the assistant can do.

    ``key_pool`` caches provider keys and publishes them into the process
    environment for litellm; without this, a ``vault_set`` would be correct and
    invisible until somebody restarted the engine -- which is the half of the
    incident that is not about precedence.
    """
    reloaded: list[str] = []
    from robothor.engine import key_pool

    monkeypatch.setattr(
        key_pool,
        "reload_provider_keys",
        lambda: reloaded.append("yes") or key_pool.ReloadResult(reloaded=[], slots=0),
    )
    result = await HANDLERS["vault_set"](
        {"key": "providers/github/api_key", "value": "ghp_FAKE3333"}, _ctx()
    )
    assert result["success"] is True
    assert reloaded, "vault_set did not reload the cached readers"


@pytest.mark.asyncio
async def test_a_write_answers_with_a_fingerprint_and_never_the_value(stored):
    result = await HANDLERS["vault_set"](
        {"key": "providers/github/api_key", "value": "ghp_FAKE3333"}, _ctx()
    )
    assert "ghp_FAKE3333" not in _flatten(result)
    assert result["fingerprint"].startswith("sha256:")
