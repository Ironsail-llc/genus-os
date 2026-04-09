"""JIRA Cloud API tool handlers for dev team operations monitoring."""

from __future__ import annotations

import base64
import os
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
    email = os.environ.get("JIRA_USER_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
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

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/rest/api/3/search/jql",
                headers=_headers(auth),
                json={
                    "jql": jql,
                    "maxResults": max_results,
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

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base_url}/rest/api/3/issue/{issue_key}",
                headers=_headers(auth),
                params={"expand": "changelog"},
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
