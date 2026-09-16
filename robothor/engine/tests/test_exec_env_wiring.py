"""The scrub has to be on the path an agent actually takes, not beside it.

A control that is built, tested and never called is the defect this project has
shipped more than any other (``controls-were-armed-but-aimed-at-nothing``,
``credential-pool-inert``, ``inert-control-case-study``). So these tests do not
call :func:`robothor.engine.exec_env.build_exec_env`. They run a REAL child
process through the ``exec`` tool handler and read what that child could see.
"""

from __future__ import annotations

import sys

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS

FAKE_GITHUB = "ghp_FAKE0000000000000000000000000000000000"
FAKE_SLACK = "xoxb-FAKE-0000-0000-fakefakefakefake"

#: Asks the child to print its own environment. Not ``env`` or ``printenv``:
#: ``robothor/engine/secret_paths.py`` refuses those outright, which is the
#: other half of this defence and must keep working.
DUMP = f"{sys.executable} -c \"import os;print(chr(10).join(f'{{k}}={{v}}' for k,v in os.environ.items()))\""


@pytest.fixture(autouse=True)
def _a_boxlike_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GITHUB)
    monkeypatch.setenv("GH_TOKEN", FAKE_GITHUB)
    monkeypatch.setenv("ROBOTHOR_SLACK_BOT_TOKEN", FAKE_SLACK)
    monkeypatch.setenv("SOME_VENDOR_PASSWORD", "fake-vendor-0000")


@pytest.fixture(autouse=True)
def _no_grants(request, monkeypatch):
    """Default: no agent has a grant. Individual tests install their own.

    ``real_grants`` opts out, for the one test that exercises the manifest
    lookup itself rather than what the handler does with its answer.
    """
    if request.node.get_closest_marker("real_grants"):
        return
    import robothor.engine.exec_env as exec_env

    monkeypatch.setattr(exec_env, "grants_for_agent", lambda agent_id, workspace="": ())


def _leaked(output: object, value: str) -> bool:
    """Whether *value* survived — as a BOOLEAN bound by the caller to a local.

    pytest's assertion rewriting prints every intermediate value in a failing
    expression, so `assert VALUE not in result["stdout"]` puts the child's
    entire environment — the real one, on whoever ran the suite — into the test
    report. Review R6; the same mistake the process note describes.
    """
    return value in str(output)


async def _run(command: str, *, agent_id: str = "researcher", workspace: str = "") -> dict:
    return await HANDLERS["exec"](
        {"command": command, "timeout": 30},
        ToolContext(agent_id=agent_id, workspace=workspace),
    )


@pytest.mark.asyncio
async def test_under_enforce_the_child_sees_no_credential(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
    result = await _run(DUMP)
    assert result.get("exit_code") == 0, result
    for value in (FAKE_GITHUB, FAKE_SLACK, "fake-vendor-0000"):
        leaked = _leaked(result["stdout"], value)
        assert not leaked, "a credential reached the child of an agent's exec"
    for name in ("GITHUB_TOKEN=", "GH_TOKEN=", "ROBOTHOR_SLACK_BOT_TOKEN="):
        present = _leaked(result["stdout"], name)
        assert not present


@pytest.mark.asyncio
async def test_under_enforce_the_child_still_has_a_working_environment(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
    result = await _run(DUMP)
    has_path = _leaked(result["stdout"], "PATH=")
    has_home = _leaked(result["stdout"], "HOME=")
    assert has_path, "a child with no PATH cannot run anything"
    assert has_home


@pytest.mark.asyncio
async def test_under_off_nothing_changes(monkeypatch):
    """The upgrade path. An existing install keeps working exactly as before
    until its operator has read the observe report and chosen."""
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "off")
    result = await _run(DUMP)
    present = _leaked(result["stdout"], FAKE_GITHUB)
    assert present


@pytest.mark.asyncio
async def test_under_observe_nothing_changes(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "observe")
    result = await _run(DUMP)
    assert FAKE_GITHUB in result["stdout"]


@pytest.mark.asyncio
async def test_a_granted_agent_gets_exactly_its_grant(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")

    import robothor.engine.exec_env as exec_env

    monkeypatch.setattr(
        exec_env,
        "grants_for_agent",
        lambda agent_id, workspace="": ("GITHUB_TOKEN",) if agent_id == "devops" else (),
    )

    granted = await _run(DUMP, agent_id="devops")
    injected = _leaked(granted["stdout"], f"GITHUB_TOKEN={FAKE_GITHUB}")
    slack_leaked = _leaked(granted["stdout"], FAKE_SLACK)
    assert injected
    assert not slack_leaked, "a grant is one name, not an amnesty"

    ungranted = await _run(DUMP, agent_id="researcher")
    inherited = _leaked(ungranted["stdout"], FAKE_GITHUB)
    assert not inherited


@pytest.mark.asyncio
async def test_a_sub_agent_does_not_inherit_its_parents_grant(monkeypatch):
    """The sub-agent rule, run rather than asserted about.

    A spawned sub-agent's tool context carries the CHILD's agent id, so the
    grant lookup reads the child's manifest. ``worker`` has no ``secrets:``
    block, so its shell sees no credential even though the ``devops`` agent
    that spawned it holds one.
    """
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")

    import robothor.engine.exec_env as exec_env

    manifests = {"devops": ("GITHUB_TOKEN",), "worker": ()}
    monkeypatch.setattr(
        exec_env,
        "grants_for_agent",
        lambda agent_id, workspace="": manifests.get(agent_id, ()),
    )

    child = await _run(DUMP, agent_id="worker")
    value_leaked = _leaked(child["stdout"], FAKE_GITHUB)
    name_leaked = _leaked(child["stdout"], "GITHUB_TOKEN=")
    assert not value_leaked
    assert not name_leaked


@pytest.mark.asyncio
async def test_an_unresolvable_grant_is_noted_in_the_tool_result(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    import robothor.engine.exec_env as exec_env
    from robothor import secrets as secrets_module
    from robothor import vault

    monkeypatch.setattr(vault, "get", lambda key, **kw: None)
    monkeypatch.setattr(vault, "export_env", lambda **kw: {})
    secrets_module.reset_vault_availability()
    monkeypatch.setattr(
        exec_env, "grants_for_agent", lambda agent_id, workspace="": ("BRAVE_API_KEY",)
    )

    result = await _run("echo ok", agent_id="researcher")
    assert "BRAVE_API_KEY" in result.get("secret_grant_note", "")
    secrets_module.reset_vault_availability()


@pytest.mark.real_grants
def test_the_manifest_grant_reaches_the_handler(tmp_path, monkeypatch):
    """No monkeypatched lookup: the manifest itself is what grants.

    Pins the seam the other tests stub, so a rename or a dropped ``secrets:``
    key cannot leave those tests passing against nothing.
    """
    from robothor.engine.exec_env import grants_for_agent

    (tmp_path / "devops.yaml").write_text(
        "id: devops\nname: DevOps\nsecrets:\n  - GITHUB_TOKEN\n", encoding="utf-8"
    )
    (tmp_path / "worker.yaml").write_text("id: worker\nname: Worker\n", encoding="utf-8")

    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(tmp_path))
    grants_for_agent.cache_clear()
    assert grants_for_agent("devops") == ("GITHUB_TOKEN",)
    assert grants_for_agent("worker") == ()
    grants_for_agent.cache_clear()
