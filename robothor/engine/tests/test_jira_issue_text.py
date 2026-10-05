"""jira_get_issue(include_text=true): description and acceptance criteria as plain text."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from robothor.engine.tools.dispatch import ToolContext

_CTX = ToolContext(agent_id="test", tenant_id="test-tenant")
_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_USER_EMAIL": "user@example.com",
    "JIRA_API_TOKEN": "token123",
}


def _adf(*paragraphs: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs
        ],
    }


def _issue() -> dict:
    return {
        "key": "ABC-123",
        "names": {"customfield_10500": "Acceptance Criteria", "summary": "Summary"},
        "fields": {
            "summary": "Add a cart badge",
            "status": {"name": "In Review", "statusCategory": {"name": "In Progress"}},
            "issuetype": {"name": "Story"},
            "description": _adf("The cart shows a count.", "Hidden when empty."),
            "customfield_10500": {
                "type": "doc",
                "content": [
                    {
                        "type": "bulletList",
                        "content": [
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "Badge shows 0-99"}],
                                    }
                                ],
                            },
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "99+ above that"}],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
        },
    }


async def _get(args: dict, data: dict):
    from robothor.engine.tools.handlers.jira import _jira_get_issue

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json.return_value = data
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    with (
        patch.dict("os.environ", _ENV),
        patch("robothor.engine.tools.handlers.jira.httpx.AsyncClient", return_value=client),
    ):
        result = await _jira_get_issue(args, _CTX)
    return result, client.get.call_args


async def test_include_text_returns_description_and_acceptance_criteria():
    result, call = await _get({"issue_key": "ABC-123", "include_text": True}, _issue())
    assert "names" in call.kwargs["params"]["expand"]
    assert result["description"] == "The cart shows a count.\nHidden when empty."
    assert result["acceptance_criteria"] == "- Badge shows 0-99\n- 99+ above that"
    assert result["url"] == "https://jira.example.com/browse/ABC-123"


async def test_without_include_text_the_result_is_unchanged():
    result, call = await _get({"issue_key": "ABC-123"}, _issue())
    assert "description" not in result and "acceptance_criteria" not in result
    assert call.kwargs["params"]["expand"] == "changelog"


async def test_plain_string_fields_and_no_criteria_field():
    data = _issue()
    data["names"] = {}
    data["fields"]["description"] = "Legacy plain text"
    result, _ = await _get({"issue_key": "ABC-123", "include_text": True}, data)
    assert result["description"] == "Legacy plain text"
    assert result["acceptance_criteria"] == ""


def test_adf_text_keeps_headings_lists_and_code():
    from robothor.engine.tools.handlers.jira import adf_to_text

    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "content": [{"type": "text", "text": "Scope"}]},
            {
                "type": "orderedList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}],
                    }
                ],
            },
            {"type": "codeBlock", "content": [{"type": "text", "text": "x = 1"}]},
            {"type": "paragraph", "content": [{"type": "hardBreak"}, {"type": "text", "text": "z"}]},
        ],
    }
    assert adf_to_text(doc) == "Scope\n1. a\nx = 1\n\nz"
    assert adf_to_text(None) == ""
    assert adf_to_text("plain") == "plain"


def test_schema_offers_include_text():
    with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
        from robothor.engine.tools import ToolRegistry

        props = ToolRegistry()._schemas["jira_get_issue"]["function"]["parameters"]["properties"]
    assert props["include_text"]["type"] == "boolean"
