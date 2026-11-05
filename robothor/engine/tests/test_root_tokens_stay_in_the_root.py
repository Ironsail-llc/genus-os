"""A token stored for the root tenant is not returned to a call made in a child.

The vault binds every read to an explicit tenant. A lookup that names no tenant
reads the root's row, so a GitHub or Claude Code token the operator stored for
the root would serve every tenant's tool calls. Each lookup here takes the
tenant of the call that needs it.
"""

from __future__ import annotations

import inspect

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.engine.tools.dispatch import ToolContext

ROOT_TOKEN = "ghp_FAKE_root_only_fixture"
CHILD = "acme-cell"


@pytest.fixture
def root_only_vault(monkeypatch):
    """A vault double holding one GitHub and one Claude Code token, for the root."""
    from robothor import secrets as secrets_module
    from robothor import vault

    def get(key, *, tenant_id=None):
        if tenant_id != DEFAULT_TENANT:
            return None
        lowered = key.lower()
        return ROOT_TOKEN if ("github" in lowered or "claude_code" in lowered) else None

    monkeypatch.setattr(vault, "get", get)
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    secrets_module.reset_vault_availability()
    yield
    secrets_module.reset_vault_availability()


@pytest.fixture
def no_network(monkeypatch):
    """Any GitHub request a handler makes is recorded and refused."""
    import httpx

    from robothor.engine.tools.handlers import github_api

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(500)

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(github_api.httpx, "AsyncClient", client)
    return requests


@pytest.mark.asyncio
async def test_a_child_tenants_github_tool_does_not_get_the_roots_token(
    root_only_vault, no_network
):
    from robothor.engine.tools.handlers.github_api import HANDLERS

    result = await HANDLERS["github_list_prs"](
        {"repo": "acme/widgets"}, ToolContext(agent_id="researcher", tenant_id=CHILD)
    )
    refused = "GITHUB_TOKEN" in str(result.get("error", ""))
    assert refused
    assert no_network == []


@pytest.mark.asyncio
async def test_the_roots_own_github_tool_still_gets_its_token(root_only_vault, no_network):
    """The control: the lookup is scoped, not broken."""
    from robothor.engine.tools.handlers.github_api import HANDLERS

    await HANDLERS["github_list_prs"](
        {"repo": "acme/widgets"}, ToolContext(agent_id="researcher", tenant_id=DEFAULT_TENANT)
    )
    assert no_network != []


def test_the_pr_review_client_resolves_against_the_callers_tenant(root_only_vault):
    from robothor.engine.tools.handlers import pr_review

    assert pr_review._github(CHILD) is None
    assert pr_review._github(DEFAULT_TENANT) is not None


def test_the_github_token_lookup_has_no_tenant_default():
    """A caller that forgets the tenant fails loudly instead of reading the root."""
    from robothor.engine.tools.handlers.github_api import _get_token

    parameter = inspect.signature(_get_token).parameters.get("tenant_id")
    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


def test_a_child_tenant_gets_no_claude_code_token_from_the_root(root_only_vault):
    from robothor.engine.coding.env import resolve_oauth_token

    assert resolve_oauth_token(CHILD) is None
    assert resolve_oauth_token(DEFAULT_TENANT) == ROOT_TOKEN


def test_the_claude_code_token_lookup_has_no_tenant_default():
    from robothor.engine.coding.env import resolve_oauth_token

    parameter = inspect.signature(resolve_oauth_token).parameters["tenant_id"]
    assert parameter.default is inspect.Parameter.empty


def test_a_coding_jobs_github_token_resolves_against_the_jobs_tenant(root_only_vault, monkeypatch):
    from robothor.engine.coding import jobs

    monkeypatch.setattr(
        "robothor.engine.exec_env.grants_for_agent", lambda agent_id, workspace="": ("GITHUB_TOKEN",)
    )
    assert jobs._default_github_token("researcher", CHILD) is None
    assert jobs._default_github_token("researcher", DEFAULT_TENANT) == ROOT_TOKEN
