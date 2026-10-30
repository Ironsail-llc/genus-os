"""JIRA Cloud API tool handlers for dev team operations monitoring."""

from __future__ import annotations

import base64
import os
import re
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _get_base_url() -> str:
    return os.environ.get("JIRA_BASE_URL", "").rstrip("/")


def _get_auth_header() -> str:
    # The token goes through the accessor (vault first) so a rotation the
    # assistant performs takes effect without a restart; the e-mail is an
    # identifier, not a credential, and stays an ordinary setting read.
    from robothor.secrets import get_secret

    email = os.environ.get("JIRA_USER_EMAIL", "")
    token = get_secret("JIRA_API_TOKEN") or ""
    if not email or not token:
        return ""
    return base64.b64encode(f"{email}:{token}".encode()).decode()


def _headers(auth: str) -> dict[str, str]:
    return {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _slim_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """Extract key fields from a JIRA issue."""
    fields = issue.get("fields") or {}
    assignee = fields.get("assignee") or {}
    status = fields.get("status") or {}
    issuetype = fields.get("issuetype") or {}
    priority = fields.get("priority") or {}

    # Story points: try common custom field names
    story_points = (
        fields.get("story_points")
        or fields.get("customfield_10016")  # Jira Cloud default
        or fields.get("customfield_10028")  # Alternative
    )

    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "status": status.get("name", ""),
        "status_category": (status.get("statusCategory") or {}).get("name", ""),
        "assignee": assignee.get("displayName", "Unassigned"),
        "assignee_email": assignee.get("emailAddress", ""),
        "issue_type": issuetype.get("name", ""),
        "priority": priority.get("name", ""),
        "story_points": story_points,
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
        "resolved": fields.get("resolutiondate", ""),
        "labels": fields.get("labels", []),
    }


_INLINE_ATTR = {"mention": "text", "emoji": "text", "inlineCard": "url", "status": "text"}


def _adf_inline(node: Any) -> str:
    if not isinstance(node, dict):
        return str(node or "")
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind in _INLINE_ATTR:
        return str((node.get("attrs") or {}).get(_INLINE_ATTR[kind]) or "")
    return "".join(_adf_inline(c) for c in node.get("content") or [])


def _adf_list(node: dict[str, Any], ordered: bool) -> list[str]:
    lines: list[str] = []
    for n, item in enumerate(node.get("content") or [], start=1):
        body = _adf_blocks(item.get("content") or []) if isinstance(item, dict) else []
        marker = f"{n}. " if ordered else "- "
        for k, line in enumerate(body or [""]):
            lines.append((marker if k == 0 else " " * len(marker)) + line)
    return lines


def _adf_blocks(nodes: list[Any]) -> list[str]:
    lines: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            lines.append(str(node))
            continue
        kind = node.get("type")
        if kind in ("bulletList", "orderedList"):
            lines += _adf_list(node, ordered=kind == "orderedList")
        elif kind == "blockquote":
            lines += ["> " + line for line in _adf_blocks(node.get("content") or [])]
        elif kind == "table":
            for row in node.get("content") or []:
                cells = [
                    " ".join(_adf_blocks(cell.get("content") or []))
                    for cell in (row.get("content") or [])
                    if isinstance(cell, dict)
                ]
                lines.append(" | ".join(cells))
        elif kind == "rule":
            lines.append("---")
        elif kind in ("paragraph", "heading", "codeBlock") or kind in _INLINE_ATTR:
            lines.append(_adf_inline(node))
        elif node.get("content"):
            lines += _adf_blocks(node.get("content") or [])
        else:
            lines.append(_adf_inline(node))
    return lines


def adf_to_text(value: Any) -> str:
    """Plain text from a Jira field: Atlassian Document Format, a string, or None."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(_adf_blocks(value)).strip()
    if isinstance(value, dict):
        if value.get("type") == "doc" or value.get("content") is not None:
            return "\n".join(_adf_blocks(value.get("content") or [])).strip()
        return _adf_inline(value).strip()
    return str(value).strip()


def _acceptance_criteria(data: dict[str, Any]) -> str:
    """Every field whose display name mentions acceptance criteria, as text."""
    names = data.get("names") or {}
    fields = data.get("fields") or {}
    parts = [
        adf_to_text(fields.get(fid))
        for fid, name in names.items()
        if "acceptance" in str(name).lower() and fields.get(fid)
    ]
    return "\n\n".join(p for p in parts if p)


def _extract_cycle_time(issue: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract status transitions from issue changelog."""
    transitions: list[dict[str, Any]] = []
    changelog = issue.get("changelog") or {}
    for history in changelog.get("histories") or []:
        created = history.get("created", "")
        transitions.extend(
            {
                "timestamp": created,
                "from_status": item.get("fromString", ""),
                "to_status": item.get("toString", ""),
            }
            for item in history.get("items") or []
            if item.get("field") == "status"
        )
    return transitions


@_handler("jira_search")
async def _jira_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search JIRA issues using JQL."""
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return {
            "error": "JIRA credentials not configured (JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN)"
        }

    jql = args.get("jql", "")
    if not jql:
        return {"error": "jql is required"}

    max_results = min(args.get("max_results", 50), 100)
    body: dict[str, Any] = {"jql": jql, "maxResults": max_results}
    if args.get("next_page_token"):
        body["nextPageToken"] = args["next_page_token"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/rest/api/3/search/jql",
                headers=_headers(auth),
                json={
                    **body,
                    "fields": [
                        "summary",
                        "status",
                        "assignee",
                        "issuetype",
                        "priority",
                        "created",
                        "updated",
                        "resolutiondate",
                        "labels",
                        "story_points",
                        "customfield_10016",
                        "customfield_10028",
                    ],
                },
            )
            if resp.status_code == 400:
                return {"error": f"Invalid JQL: {resp.text}"}
            if resp.status_code == 401:
                return {"error": "JIRA authentication failed — check API token"}
            if resp.status_code == 429:
                return {"error": "JIRA rate limit exceeded. Try again later."}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    issues = [_slim_issue(i) for i in (data.get("issues") or [])]
    return {
        "issues": issues,
        "count": len(issues),
        "total": data.get("total", len(issues)),
        "is_last": data.get("isLast", True),
        "next_page_token": data.get("nextPageToken"),
    }


@_handler("jira_get_issue")
async def _jira_get_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get a single JIRA issue with changelog for cycle time analysis."""
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return {
            "error": "JIRA credentials not configured (JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN)"
        }

    issue_key = args.get("issue_key", "")
    if not issue_key:
        return {"error": "issue_key is required"}
    include_text = args.get("include_text") is True

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base_url}/rest/api/3/issue/{issue_key}",
                headers=_headers(auth),
                params={"expand": "changelog,names" if include_text else "changelog"},
            )
            if resp.status_code == 404:
                return {"error": f"Issue {issue_key} not found"}
            if resp.status_code == 401:
                return {"error": "JIRA authentication failed — check API token"}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    result = _slim_issue(data)
    result["transitions"] = _extract_cycle_time(data)
    if include_text:
        fields = data.get("fields") or {}
        result["description"] = adf_to_text(fields.get("description"))
        result["acceptance_criteria"] = _acceptance_criteria(data)
        result["url"] = f"{base_url}/browse/{data.get('key') or issue_key}"
    return result


@_handler("jira_get_sprint")
async def _jira_get_sprint(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get active or recent sprint info for a board."""
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return {
            "error": "JIRA credentials not configured (JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN)"
        }

    board_id = args.get("board_id")
    if not board_id:
        return {"error": "board_id is required"}

    state = args.get("state", "active")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get sprints for the board
            resp = await client.get(
                f"{base_url}/rest/agile/1.0/board/{board_id}/sprint",
                headers=_headers(auth),
                params={"state": state, "maxResults": 5},
            )
            if resp.status_code == 404:
                return {"error": f"Board {board_id} not found"}
            if resp.status_code == 401:
                return {"error": "JIRA authentication failed — check API token"}
            resp.raise_for_status()
            sprint_data = resp.json()

            sprints = sprint_data.get("values") or []
            if not sprints:
                return {"error": f"No {state} sprints found for board {board_id}"}

            # Get issues for the most recent sprint
            sprint = sprints[-1]
            sprint_id = sprint["id"]

            resp = await client.get(
                f"{base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
                headers=_headers(auth),
                params={
                    "maxResults": 100,
                    "fields": "summary,status,assignee,issuetype,story_points,customfield_10016,customfield_10028",
                },
            )
            resp.raise_for_status()
            issue_data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    issues = [_slim_issue(i) for i in (issue_data.get("issues") or [])]
    total_points = sum(i.get("story_points") or 0 for i in issues)
    done_points = sum(
        (i.get("story_points") or 0) for i in issues if i.get("status_category") == "Done"
    )

    return {
        "sprint": {
            "id": sprint_id,
            "name": sprint.get("name", ""),
            "state": sprint.get("state", ""),
            "start_date": sprint.get("startDate", ""),
            "end_date": sprint.get("endDate", ""),
            "goal": sprint.get("goal", ""),
        },
        "issues": issues,
        "total_issues": len(issues),
        "total_points": total_points,
        "completed_points": done_points,
        "completion_rate": round(done_points / total_points * 100, 1) if total_points else 0,
    }


@_handler("jira_get_board_velocity")
async def _jira_get_board_velocity(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get velocity data (committed vs completed points) for last N sprints."""
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return {
            "error": "JIRA credentials not configured (JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN)"
        }

    board_id = args.get("board_id")
    if not board_id:
        return {"error": "board_id is required"}

    num_sprints = min(args.get("num_sprints", 5), 10)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Get closed sprints
            resp = await client.get(
                f"{base_url}/rest/agile/1.0/board/{board_id}/sprint",
                headers=_headers(auth),
                params={"state": "closed", "maxResults": num_sprints},
            )
            if resp.status_code == 404:
                return {"error": f"Board {board_id} not found"}
            resp.raise_for_status()
            sprint_data = resp.json()

            sprints = sprint_data.get("values") or []
            velocity = []

            for sprint in sprints[-num_sprints:]:
                sprint_id = sprint["id"]
                resp = await client.get(
                    f"{base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
                    headers=_headers(auth),
                    params={
                        "maxResults": 100,
                        "fields": "status,story_points,customfield_10016,customfield_10028",
                    },
                )
                resp.raise_for_status()
                issue_data = resp.json()

                issues = [_slim_issue(i) for i in (issue_data.get("issues") or [])]
                committed = sum(i.get("story_points") or 0 for i in issues)
                completed = sum(
                    (i.get("story_points") or 0)
                    for i in issues
                    if i.get("status_category") == "Done"
                )
                velocity.append(
                    {
                        "sprint_name": sprint.get("name", ""),
                        "start_date": sprint.get("startDate", ""),
                        "end_date": sprint.get("endDate", ""),
                        "committed_points": committed,
                        "completed_points": completed,
                        "completion_rate": round(completed / committed * 100, 1)
                        if committed
                        else 0,
                    }
                )
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    avg_velocity = (
        round(sum(v["completed_points"] for v in velocity) / len(velocity), 1) if velocity else 0
    )

    return {
        "board_id": board_id,
        "sprints": velocity,
        "average_velocity": avg_velocity,
        "sprint_count": len(velocity),
    }


@_handler("jira_list_boards")
async def _jira_list_boards(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List available JIRA boards."""
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return {
            "error": "JIRA credentials not configured (JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN)"
        }

    project_key = args.get("project_key", "")

    try:
        params: dict[str, Any] = {"maxResults": 50}
        if project_key:
            params["projectKeyOrId"] = project_key

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base_url}/rest/agile/1.0/board",
                headers=_headers(auth),
                params=params,
            )
            if resp.status_code == 401:
                return {"error": "JIRA authentication failed — check API token"}
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    boards = [
        {
            "id": b.get("id"),
            "name": b.get("name", ""),
            "type": b.get("type", ""),
            "project_key": (b.get("location") or {}).get("projectKey", ""),
            "project_name": (b.get("location") or {}).get("projectName", ""),
        }
        for b in (data.get("values") or [])
    ]

    return {
        "boards": boards,
        "count": len(boards),
    }


# ─── Write tools ────────────────────────────────────────────────────

_NO_CREDENTIALS = {
    "error": "JIRA credentials not configured (JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN)"
}

# A key goes into the URL path, and httpx normalises `..` segments, so anything
# looser would let a model-supplied key aim this tool's body at another endpoint.
_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]*-\d+|\d+")
_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]*")


def _refuse_benchmark(name: str, ctx: ToolContext) -> dict[str, Any] | None:
    """Defence in depth beside EXTERNAL_SIDE_EFFECT_TOOLS: never touch the real tracker."""
    if ctx.is_benchmark:
        return {"error": f"{name} is refused on a benchmark run"}
    return None


def _bad_key(field: str, value: str) -> dict[str, Any] | None:
    if _ISSUE_KEY.fullmatch(value):
        return None
    return {"error": f"{field} must be an issue key like ABC-123 or a numeric id, got {value!r}"}


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text → Atlassian Document Format: blank lines split paragraphs."""
    paragraphs = []
    for block in str(text).split("\n\n"):
        lines = block.strip("\n").split("\n")
        if not any(line.strip() for line in lines):
            continue
        content: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _write_error(resp: Any) -> dict[str, Any] | None:
    """Map a failed write response to an error the model can act on."""
    if resp.status_code == 401:
        return {"error": "JIRA authentication failed — check API token"}
    if resp.status_code == 403:
        return {"error": "JIRA refused: the account lacks permission for this action"}
    if resp.status_code == 404:
        return {"error": "JIRA issue or project not found"}
    if resp.status_code == 400:
        return {"error": f"JIRA rejected the request: {resp.text}"}
    if resp.status_code == 429:
        return {"error": "JIRA rate limit exceeded. Try again later."}
    return None


@_handler("jira_create_issue")
async def _jira_create_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a JIRA issue (Task by default) in any project the account can write to."""
    if refused := _refuse_benchmark("jira_create_issue", ctx):
        return refused
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return dict(_NO_CREDENTIALS)

    project_key = str(args.get("project_key") or "").strip()
    summary = str(args.get("summary") or "").strip()
    if not project_key or not summary:
        return {"error": "project_key and summary are required"}
    if not _PROJECT_KEY.fullmatch(project_key):
        return {"error": f"project_key must be a project key like ABC, got {project_key!r}"}
    parent_key = str(args.get("parent_key") or "").strip()
    if parent_key and (error := _bad_key("parent_key", parent_key)):
        return error

    fields: dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": args.get("issue_type") or "Task"},
    }
    if args.get("description"):
        fields["description"] = text_to_adf(args["description"])
    if args.get("labels"):
        fields["labels"] = list(args["labels"])
    if parent_key:
        fields["parent"] = {"key": parent_key}
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/rest/api/3/issue", headers=_headers(auth), json={"fields": fields}
            )
            if error := _write_error(resp):
                return error
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # Never connected, so nothing was sent: a retry is safe.
        return {"error": f"JIRA request failed: {e}"}
    except httpx.TransportError as e:
        # The POST may have landed before the timeout or disconnect; a blind
        # retry is how a tracker gets two copies of the same ticket.
        return {
            "error": (
                f"JIRA request failed ({type(e).__name__}): the issue may have been created. "
                f"Before retrying, jira_search project {project_key} for the summary "
                f"{summary!r} and only create it if nothing matches."
            )
        }
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    key = data.get("key", "")
    return {"key": key, "id": data.get("id", ""), "url": f"{base_url}/browse/{key}"}


@_handler("jira_add_comment")
async def _jira_add_comment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Add a comment to a JIRA issue."""
    if refused := _refuse_benchmark("jira_add_comment", ctx):
        return refused
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return dict(_NO_CREDENTIALS)

    issue_key = str(args.get("issue_key") or "").strip()
    body = str(args.get("body") or "").strip()
    if not issue_key or not body:
        return {"error": "issue_key and body are required"}
    if error := _bad_key("issue_key", issue_key):
        return error

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/rest/api/3/issue/{issue_key}/comment",
                headers=_headers(auth),
                json={"body": text_to_adf(body)},
            )
            if error := _write_error(resp):
                return error
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    return {"issue_key": issue_key, "comment_id": str(data.get("id", ""))}


@_handler("jira_transition_issue")
async def _jira_transition_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Move a JIRA issue to a status (matched by target status or transition name)."""
    if refused := _refuse_benchmark("jira_transition_issue", ctx):
        return refused
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return dict(_NO_CREDENTIALS)

    issue_key = str(args.get("issue_key") or "").strip()
    wanted = str(args.get("status") or "").strip().lower()
    if not issue_key or not wanted:
        return {"error": "issue_key and status are required"}
    if error := _bad_key("issue_key", issue_key):
        return error

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            url = f"{base_url}/rest/api/3/issue/{issue_key}/transitions"
            resp = await client.get(url, headers=_headers(auth))
            if error := _write_error(resp):
                return error
            resp.raise_for_status()
            transitions = resp.json().get("transitions") or []
            match = next(
                (
                    t
                    for t in transitions
                    if wanted
                    in {
                        str((t.get("to") or {}).get("name", "")).lower(),
                        str(t.get("name", "")).lower(),
                    }
                ),
                None,
            )
            if match is None:
                options = sorted({(t.get("to") or {}).get("name", "") for t in transitions})
                return {
                    "error": f"No transition to '{args.get('status')}' from the current status; "
                    f"available: {', '.join(o for o in options if o) or 'none'}"
                }
            resp = await client.post(
                url, headers=_headers(auth), json={"transition": {"id": match["id"]}}
            )
            if error := _write_error(resp):
                return error
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    return {"issue_key": issue_key, "status": (match.get("to") or {}).get("name", "")}


@_handler("jira_update_issue")
async def _jira_update_issue(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Edit a JIRA issue's summary, description, labels or priority."""
    if refused := _refuse_benchmark("jira_update_issue", ctx):
        return refused
    base_url = _get_base_url()
    auth = _get_auth_header()
    if not base_url or not auth:
        return dict(_NO_CREDENTIALS)

    issue_key = str(args.get("issue_key") or "").strip()
    if not issue_key:
        return {"error": "issue_key is required"}
    if error := _bad_key("issue_key", issue_key):
        return error

    fields: dict[str, Any] = {}
    if args.get("summary"):
        fields["summary"] = str(args["summary"])
    if args.get("description"):
        fields["description"] = text_to_adf(args["description"])
    if args.get("labels") is not None:
        fields["labels"] = list(args["labels"])
    if args.get("priority"):
        fields["priority"] = {"name": args["priority"]}
    if not fields:
        return {"error": "nothing to update: pass summary, description, labels or priority"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(
                f"{base_url}/rest/api/3/issue/{issue_key}",
                headers=_headers(auth),
                json={"fields": fields},
            )
            if error := _write_error(resp):
                return error
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return {"error": f"JIRA API error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"JIRA request failed: {e}"}

    return {"issue_key": issue_key, "updated": sorted(fields)}
