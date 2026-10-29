"""Tests for the JIRA write tools: create, comment, transition, update."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")

_ENV = {
    "JIRA_BASE_URL": "https://test.atlassian.net",
    "JIRA_USER_EMAIL": "user@example.com",
    "JIRA_API_TOKEN": "token123",
}

_WRITE_TOOLS = [
    "jira_create_issue",
    "jira_add_comment",
    "jira_transition_issue",
    "jira_update_issue",
]


def _resp(status: int = 200, payload: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload if payload is not None else {}
    resp.text = ""
    return resp


def _client(**methods: object) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        setattr(client, name, value)
    return client


def _patched(client: AsyncMock):
    return patch("robothor.engine.tools.handlers.jira.httpx.AsyncClient", return_value=client)


class TestJiraWriteRegistration:
    def test_write_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for name in _WRITE_TOOLS:
                assert name in registry._schemas, f"{name} not in registry"

    def test_write_tools_not_readonly(self):
        from robothor.engine.tools import JIRA_TOOLS, READONLY_TOOLS

        for name in _WRITE_TOOLS:
            assert name in JIRA_TOOLS
            assert name not in READONLY_TOOLS


class TestCreateIssue:
    @pytest.mark.asyncio
    async def test_creates_issue_with_adf_description(self):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        post = AsyncMock(return_value=_resp(201, {"id": "1", "key": "GEN-40"}))
        client = _client(post=post)
        with patch.dict("os.environ", _ENV), _patched(client):
            result = await _jira_create_issue(
                {
                    "project_key": "GEN",
                    "summary": "Fix the thing",
                    "description": "First line\n\nSecond paragraph",
                    "issue_type": "Bug",
                    "labels": ["robothor"],
                    "parent_key": "GEN-39",
                },
                _CTX,
            )

        assert result["key"] == "GEN-40"
        assert result["url"] == "https://test.atlassian.net/browse/GEN-40"
        body = post.call_args.kwargs["json"]
        fields = body["fields"]
        assert fields["project"] == {"key": "GEN"}
        assert fields["summary"] == "Fix the thing"
        assert fields["issuetype"] == {"name": "Bug"}
        assert fields["labels"] == ["robothor"]
        assert fields["parent"] == {"key": "GEN-39"}
        assert fields["description"]["type"] == "doc"
        assert len(fields["description"]["content"]) == 2

    @pytest.mark.asyncio
    async def test_defaults_to_task(self):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        post = AsyncMock(return_value=_resp(201, {"key": "GEN-41"}))
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            await _jira_create_issue({"project_key": "GEN", "summary": "x"}, _CTX)
        assert post.call_args.kwargs["json"]["fields"]["issuetype"] == {"name": "Task"}

    @pytest.mark.asyncio
    async def test_requires_project_and_summary(self):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        with patch.dict("os.environ", _ENV):
            assert "error" in await _jira_create_issue({"summary": "x"}, _CTX)
            assert "error" in await _jira_create_issue({"project_key": "GEN"}, _CTX)

    @pytest.mark.asyncio
    async def test_reports_jira_validation_error(self):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        resp = _resp(400, {"errors": {"issuetype": "invalid"}})
        resp.text = '{"errors":{"issuetype":"invalid"}}'
        with patch.dict("os.environ", _ENV), _patched(_client(post=AsyncMock(return_value=resp))):
            result = await _jira_create_issue(
                {"project_key": "GEN", "summary": "x", "issue_type": "Nope"}, _CTX
            )
        assert "issuetype" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_credentials(self):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        with patch.dict("os.environ", {}, clear=True):
            result = await _jira_create_issue({"project_key": "GEN", "summary": "x"}, _CTX)
        assert "credentials" in result["error"]


class TestAddComment:
    @pytest.mark.asyncio
    async def test_adds_comment(self):
        from robothor.engine.tools.handlers.jira import _jira_add_comment

        post = AsyncMock(return_value=_resp(201, {"id": "100"}))
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            result = await _jira_add_comment({"issue_key": "GEN-1", "body": "Done."}, _CTX)
        assert result == {"issue_key": "GEN-1", "comment_id": "100"}
        assert post.call_args.args[0].endswith("/rest/api/3/issue/GEN-1/comment")
        assert post.call_args.kwargs["json"]["body"]["type"] == "doc"


class TestTransitionIssue:
    @pytest.mark.asyncio
    async def test_transitions_by_status_name(self):
        from robothor.engine.tools.handlers.jira import _jira_transition_issue

        get = AsyncMock(
            return_value=_resp(
                200,
                {
                    "transitions": [
                        {"id": "11", "name": "Start", "to": {"name": "In Progress"}},
                        {"id": "31", "name": "Finish", "to": {"name": "Done"}},
                    ]
                },
            )
        )
        post = AsyncMock(return_value=_resp(204))
        with patch.dict("os.environ", _ENV), _patched(_client(get=get, post=post)):
            result = await _jira_transition_issue({"issue_key": "GEN-1", "status": "done"}, _CTX)
        assert result["status"] == "Done"
        assert post.call_args.kwargs["json"] == {"transition": {"id": "31"}}

    @pytest.mark.asyncio
    async def test_unknown_status_lists_options(self):
        from robothor.engine.tools.handlers.jira import _jira_transition_issue

        get = AsyncMock(
            return_value=_resp(
                200, {"transitions": [{"id": "11", "name": "Start", "to": {"name": "In Progress"}}]}
            )
        )
        with patch.dict("os.environ", _ENV), _patched(_client(get=get, post=AsyncMock())):
            result = await _jira_transition_issue({"issue_key": "GEN-1", "status": "Done"}, _CTX)
        assert "In Progress" in result["error"]


class TestUpdateIssue:
    @pytest.mark.asyncio
    async def test_updates_fields(self):
        from robothor.engine.tools.handlers.jira import _jira_update_issue

        put = AsyncMock(return_value=_resp(204))
        with patch.dict("os.environ", _ENV), _patched(_client(put=put)):
            result = await _jira_update_issue(
                {"issue_key": "GEN-1", "summary": "New", "labels": ["a"], "description": "d"},
                _CTX,
            )
        assert result["updated"] == ["description", "labels", "summary"]
        fields = put.call_args.kwargs["json"]["fields"]
        assert fields["summary"] == "New"
        assert fields["description"]["type"] == "doc"

    @pytest.mark.asyncio
    async def test_requires_a_field(self):
        from robothor.engine.tools.handlers.jira import _jira_update_issue

        with patch.dict("os.environ", _ENV):
            result = await _jira_update_issue({"issue_key": "GEN-1"}, _CTX)
        assert "error" in result


# ─── gates ─────────────────────────────────────────────────────────


class TestJiraWritesAreOptIn:
    """They write to the tracker as the instance's account, like the GitHub review writes."""

    def test_write_tools_are_in_opt_in_tools(self):
        from robothor.engine.tools.constants import JIRA_WRITE_TOOLS, OPT_IN_TOOLS

        assert set(_WRITE_TOOLS) == JIRA_WRITE_TOOLS
        assert JIRA_WRITE_TOOLS <= OPT_IN_TOOLS

    def test_a_default_agent_gets_the_reads_not_the_writes(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.tools.registry import ToolRegistry

        names = set(ToolRegistry().get_tool_names(AgentConfig(id="probe", name="Probe")))
        assert not set(_WRITE_TOOLS) & names
        assert {"jira_search", "jira_get_issue"} <= names

    def test_tools_allowed_grants_them(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.tools.registry import ToolRegistry

        config = AgentConfig(id="w", name="W", tools_allowed=["jira_search", *_WRITE_TOOLS])
        assert set(_WRITE_TOOLS) <= set(ToolRegistry().get_tool_names(config))

    def test_tools_opt_in_adds_them_to_the_default_set(self):
        from robothor.engine.models import AgentConfig
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        default = set(registry.get_tool_names(AgentConfig(id="probe", name="Probe")))
        config = AgentConfig(id="probe", name="Probe", tools_opt_in=list(_WRITE_TOOLS))
        assert set(registry.get_tool_names(config)) == default | set(_WRITE_TOOLS)


_VALID_ARGS = {
    "jira_create_issue": {"project_key": "GEN", "summary": "x"},
    "jira_add_comment": {"issue_key": "GEN-1", "body": "hi"},
    "jira_transition_issue": {"issue_key": "GEN-1", "status": "Done"},
    "jira_update_issue": {"issue_key": "GEN-1", "summary": "y"},
}


class TestBenchmarkRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", _WRITE_TOOLS)
    async def test_each_write_refuses_a_benchmark_run(self, tool):
        from robothor.engine.tools.handlers.jira import HANDLERS

        ctx = ToolContext(agent_id="test", tenant_id="test-tenant", is_benchmark=True)
        client = _client(get=AsyncMock(), post=AsyncMock(), put=AsyncMock())
        with patch.dict("os.environ", _ENV), _patched(client) as cls:
            result = await HANDLERS[tool](dict(_VALID_ARGS[tool]), ctx)
        assert "benchmark" in result["error"]
        cls.assert_not_called()


_BAD_KEYS = ["GEN-1/../../project/ABC", "gen-1", "GEN", "GEN-1?x=1", "GEN-1#a", "../1", "GEN 1"]


class TestKeyValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool", ["jira_add_comment", "jira_transition_issue", "jira_update_issue"]
    )
    @pytest.mark.parametrize("bad", _BAD_KEYS)
    async def test_a_malformed_issue_key_never_reaches_jira(self, tool, bad):
        from robothor.engine.tools.handlers.jira import HANDLERS

        client = _client(get=AsyncMock(), post=AsyncMock(), put=AsyncMock())
        with patch.dict("os.environ", _ENV), _patched(client) as cls:
            result = await HANDLERS[tool]({**_VALID_ARGS[tool], "issue_key": bad}, _CTX)
        assert "issue_key" in result["error"]
        cls.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("good", ["GEN-1", "AB_2-345", "10042"])
    async def test_keys_and_numeric_ids_are_accepted(self, good):
        from robothor.engine.tools.handlers.jira import _jira_add_comment

        post = AsyncMock(return_value=_resp(201, {"id": "1"}))
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            result = await _jira_add_comment({"issue_key": good, "body": "x"}, _CTX)
        assert "error" not in result
        assert post.call_args.args[0].endswith(f"/issue/{good}/comment")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", _BAD_KEYS)
    async def test_a_malformed_parent_key_is_refused(self, bad):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        post = AsyncMock()
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            result = await _jira_create_issue(
                {"project_key": "GEN", "summary": "x", "parent_key": bad}, _CTX
            )
        assert "parent_key" in result["error"]
        post.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["gen", "GEN-1", "G EN", "GEN/../x", "1GEN"])
    async def test_a_malformed_project_key_is_refused(self, bad):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        post = AsyncMock()
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            result = await _jira_create_issue({"project_key": bad, "summary": "x"}, _CTX)
        assert "project_key" in result["error"]
        post.assert_not_called()


class TestCreateTransportFailure:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("read timed out"),
            httpx.RemoteProtocolError("server disconnected"),
        ],
    )
    async def test_says_the_issue_may_exist_and_to_search_before_retrying(self, exc):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        post = AsyncMock(side_effect=exc)
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            result = await _jira_create_issue({"project_key": "GEN", "summary": "Fix it"}, _CTX)
        assert "may have been created" in result["error"]
        assert "jira_search" in result["error"]
        assert "Fix it" in result["error"]

    @pytest.mark.asyncio
    async def test_a_refused_connection_is_a_plain_failure(self):
        from robothor.engine.tools.handlers.jira import _jira_create_issue

        post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch.dict("os.environ", _ENV), _patched(_client(post=post)):
            result = await _jira_create_issue({"project_key": "GEN", "summary": "x"}, _CTX)
        assert "may have been created" not in result["error"]
