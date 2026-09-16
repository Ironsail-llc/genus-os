"""Who may touch credentials — on a key RBAC does not read.

Review findings C3 and I3, both of which turned the first cut's fail-closed
gate into something worse than an open one.

**C3.** The gate keyed on the manifest's ``role:``, which is the SAME field
``resolve_service_role`` feeds to ``check_tool_permission``. So the prescribed
fix — "add ``role: main`` to main.yaml" — would have made every heartbeat, cron
and spawned run of ``main`` call ``check_tool_permission(user_role="main")``,
find no ``role_permissions`` row for ``main``, and be denied EVERY tool. On a
box running ``ROBOTHOR_RBAC_MODE=enforce`` that is an outage instruction, and
``main`` could not both keep its tools and hold the vault tools.

**I3.** The gate read ``config.service_role``, which ``resolve_service_role``
fills in from ``ROBOTHOR_DEFAULT_SERVICE_ROLE`` when the manifest declares
nothing. That fleet-wide knob — which the SERVICE_ROLES runbook tells operators
to use — was therefore a one-line grant of ``vault_set`` to every agent on the
instance, sub-agents included.

So the tier is its own manifest key, under ``v2:``, that nothing else
interprets:

    v2:
      credentials: operator

It defaults to absent, absence is a refusal, and it is read from the DECLARED
manifest rather than from anything a default can fill in.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.handlers.vault import HANDLERS

VAULT_TOOLS = ["vault_get", "vault_set", "vault_test", "vault_list", "vault_delete"]


def _ctx(agent_id: str = "main"):
    from robothor.engine.tools.dispatch import ToolContext

    return ToolContext(agent_id=agent_id, tenant_id="default")


def _args() -> dict:
    return {"key": "providers/github/api_key", "value": "ghp_FAKE0000", "category": "credential"}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A manifest directory, and nothing else on the machine."""
    from robothor.engine.exec_env import grants_for_agent
    from robothor.engine.tools.handlers import vault as vault_tools

    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(tmp_path))
    _clear = getattr(vault_tools.credential_tier, "cache_clear", lambda: None)
    _clear()
    grants_for_agent.cache_clear()
    yield tmp_path
    getattr(vault_tools.credential_tier, "cache_clear", lambda: None)()
    grants_for_agent.cache_clear()


def _write(fleet, agent_id: str, body: str) -> None:
    (fleet / f"{agent_id}.yaml").write_text(f"id: {agent_id}\nname: {agent_id}\n{body}", "utf-8")


# ── C3: the tier is not the RBAC role ────────────────────────────────────────


def test_the_tier_key_is_not_the_rbac_role(fleet):
    """The whole of C3 in one assertion: an agent can hold the vault tools
    while its RBAC role stays whatever the fleet's policy rows actually seed."""
    from robothor.engine.config import EngineConfig, load_agent_config
    from robothor.engine.tools.handlers.vault import credential_tier

    _write(fleet, "main", "v2:\n  credentials: operator\n")
    config = load_agent_config("main", EngineConfig.from_env().manifest_dir)

    assert credential_tier("main") == "operator"
    assert config.service_role != "main", (
        "the credential tier leaked into the RBAC role — setting it would deny "
        "this agent every tool under RBAC enforce, because no role_permissions "
        "row exists for it"
    )


def test_an_operator_tier_agent_may_use_the_vault_tools(fleet):
    from robothor.engine.tools.handlers.vault import _operator_denial

    _write(fleet, "main", "v2:\n  credentials: operator\n")
    assert _operator_denial(_ctx("main"), "vault_set") is None


@pytest.mark.parametrize("tool", VAULT_TOOLS)
@pytest.mark.asyncio
async def test_an_agent_without_the_tier_is_refused(fleet, tool):
    _write(fleet, "worker", "")
    result = await HANDLERS[tool](_args(), _ctx("worker"))
    assert "error" in result
    assert result["denied_by"] == "credential_tier"


@pytest.mark.asyncio
async def test_the_refusal_names_the_key_to_set(fleet):
    """A refusal an operator cannot act on is a support ticket."""
    _write(fleet, "worker", "")
    result = await HANDLERS["vault_set"](_args(), _ctx("worker"))
    assert "credentials: operator" in result["error"]


# ── I3: the fleet-wide role default must not grant it ────────────────────────


def test_the_default_service_role_knob_does_not_grant_the_tier(fleet, monkeypatch):
    """``ROBOTHOR_DEFAULT_SERVICE_ROLE`` moves the whole fleet's RBAC posture.

    It must not be able to move the credential posture with it: the runbook
    tells operators to set it, and setting it to anything operator-shaped was a
    one-line grant of ``vault_set`` to every sub-agent on the box.
    """
    from robothor.engine.tools.handlers.vault import _operator_denial, credential_tier

    _write(fleet, "worker", "")
    for role in ("operator", "owner", "admin", "main"):
        monkeypatch.setenv("ROBOTHOR_DEFAULT_SERVICE_ROLE", role)
        credential_tier.cache_clear()  # noqa: B909 - the cache is the seam under test
        assert credential_tier("worker") == "", f"the default role {role!r} granted the tier"
        assert _operator_denial(_ctx("worker"), "vault_set") is not None


def test_a_role_of_main_does_not_grant_the_tier(fleet):
    """The old gate keyed on exactly this. It must now mean nothing here, or
    C3's outage instruction is still the way in."""
    from robothor.engine.tools.handlers.vault import credential_tier

    _write(fleet, "impostor", "role: main\n")
    assert credential_tier("impostor") == ""


# ── fail-closed, still ────────────────────────────────────────────────────────


def test_an_unknown_tier_value_is_a_refusal(fleet):
    from robothor.engine.tools.handlers.vault import _operator_denial

    _write(fleet, "main", "v2:\n  credentials: sort-of\n")
    assert _operator_denial(_ctx("main"), "vault_set") is not None


def test_an_unreadable_manifest_is_a_refusal(fleet, monkeypatch):
    from robothor.engine.tools.handlers import vault as vault_tools

    def boom(agent_id):
        raise RuntimeError("manifest directory is gone")

    monkeypatch.setattr(vault_tools, "credential_tier", boom)
    assert vault_tools._operator_denial(_ctx("main"), "vault_set") is not None


def test_an_unattributed_call_is_a_refusal(fleet):
    from robothor.engine.tools.handlers.vault import _operator_denial

    assert _operator_denial(_ctx(""), "vault_set") is not None


def test_the_manifest_schema_accepts_the_key(fleet):
    """An unknown ``v2:`` key is rejected under ``enforce``, so the schema has
    to know about this one or the opt-in breaks the manifest it is added to."""
    from robothor.engine.manifest_schema import validate

    issues = validate({"id": "main", "name": "Main", "v2": {"credentials": "operator"}})
    assert not [i for i in issues if i.code in {"unknown_key", "unknown_v2_key", "invalid_enum"}], [
        f"{i.code}: {i.message}" for i in issues
    ]
