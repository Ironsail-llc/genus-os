"""Google Workspace (gws CLI) tool handlers."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _run_gws(args: list[str], timeout: int = 30) -> dict[str, Any]:
    """Run a gws CLI command, return parsed JSON or error dict."""
    import json as _json

    try:
        proc = subprocess.run(
            ["gws"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return {
                "error": proc.stderr.strip()[:1000] or f"gws exited with code {proc.returncode}"
            }
        try:
            result: dict[str, Any] = _json.loads(proc.stdout)
            return result
        except _json.JSONDecodeError:
            return {"output": proc.stdout[:4000]}
    except subprocess.TimeoutExpired:
        return {"error": f"gws command timed out after {timeout}s"}
    except FileNotFoundError:
        return {"error": "gws CLI not found — install with: npm install -g @googleworkspace/cli"}
    except Exception as e:
        return {"error": f"gws failed: {e}"}


def _handle_gws_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Handle all gws_* tool calls by mapping to gws CLI commands."""
    import json as _json

    if name == "gws_gmail_search":
        query = args.get("query", "")
        max_results = min(args.get("max_results", 10), 100)
        params = {"userId": "me", "q": query, "maxResults": max_results}
        return _run_gws(["gmail", "users", "messages", "list", "--params", _json.dumps(params)])

    if name == "gws_gmail_get":
        message_id = args.get("message_id", "")
        thread_id = args.get("thread_id", "")
        fmt = args.get("format", "full")

        if thread_id:
            params = {"userId": "me", "id": thread_id, "format": fmt}
            return _run_gws(["gmail", "users", "threads", "get", "--params", _json.dumps(params)])
        if message_id:
            params = {"userId": "me", "id": message_id, "format": fmt}
            return _run_gws(["gmail", "users", "messages", "get", "--params", _json.dumps(params)])
        return {"error": "Either message_id or thread_id is required"}

    if name == "gws_gmail_send":
        import base64
        from email.mime.text import MIMEText

        to = args.get("to", "")
        subject = args.get("subject", "")
        body = args.get("body", "")
        cc = args.get("cc", "")
        thread_id = args.get("thread_id")
        in_reply_to = args.get("in_reply_to", "")

        msg = MIMEText(body)
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        json_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            json_body["threadId"] = thread_id

        return _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                '{"userId":"me"}',
                "--json",
                _json.dumps(json_body),
            ],
            timeout=30,
        )

    if name == "gws_gmail_modify":
        message_id = args.get("message_id", "")
        if not message_id:
            return {"error": "message_id is required"}
        add_labels = args.get("add_labels", [])
        remove_labels = args.get("remove_labels", [])
        modify_body: dict[str, Any] = {}
        if add_labels:
            modify_body["addLabelIds"] = add_labels
        if remove_labels:
            modify_body["removeLabelIds"] = remove_labels
        if not modify_body:
            return {"error": "At least one of add_labels or remove_labels is required"}
        return _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "modify",
                "--params",
                _json.dumps({"userId": "me", "id": message_id}),
                "--json",
                _json.dumps(modify_body),
            ]
        )

    if name == "gws_calendar_list":
        time_min = args.get("time_min", "")
        if not time_min:
            return {"error": "time_min is required"}
        cal_params: dict[str, Any] = {
            "calendarId": args.get("calendar_id", "primary"),
            "timeMin": time_min,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": min(args.get("max_results", 20), 250),
        }
        time_max = args.get("time_max")
        if time_max:
            cal_params["timeMax"] = time_max
        return _run_gws(["calendar", "events", "list", "--params", _json.dumps(cal_params)])

    if name == "gws_calendar_create":
        summary = args.get("summary", "")
        start = args.get("start", "")
        end = args.get("end", "")
        if not summary or not start or not end:
            return {"error": "summary, start, and end are required"}

        event_body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if args.get("description"):
            event_body["description"] = args["description"]
        if args.get("location"):
            event_body["location"] = args["location"]
        if args.get("attendees"):
            event_body["attendees"] = [{"email": e} for e in args["attendees"]]

        with_meet = args.get("with_meet", True)
        if with_meet:
            request_id = f"robothor-{summary[:20]}-{start[:10]}".replace(" ", "-")
            event_body["conferenceData"] = {
                "createRequest": {
                    "requestId": request_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        calendar_id = args.get("calendar_id", "primary")
        cal_params: dict[str, Any] = {"calendarId": calendar_id}
        if with_meet:
            cal_params["conferenceDataVersion"] = 1

        return _run_gws(
            [
                "calendar",
                "events",
                "insert",
                "--params",
                _json.dumps(cal_params),
                "--json",
                _json.dumps(event_body),
            ]
        )

    if name == "gws_calendar_delete":
        event_id = args.get("event_id", "")
        if not event_id:
            return {"error": "event_id is required"}
        calendar_id = args.get("calendar_id", "primary")
        return _run_gws(
            [
                "calendar",
                "events",
                "delete",
                "--params",
                _json.dumps({"calendarId": calendar_id, "eventId": event_id}),
            ]
        )

    if name == "gws_chat_send":
        space = args.get("space", "")
        text = args.get("text", "")
        if not space or not text:
            return {"error": "space and text are required"}
        return _run_gws(
            [
                "chat",
                "spaces",
                "messages",
                "create",
                "--params",
                _json.dumps({"parent": space}),
                "--json",
                _json.dumps({"text": text}),
            ]
        )

    if name == "gws_chat_list_spaces":
        page_size = min(args.get("page_size", 50), 1000)
        return _run_gws(
            [
                "chat",
                "spaces",
                "list",
                "--params",
                _json.dumps({"pageSize": page_size}),
            ]
        )

    if name == "gws_chat_list_messages":
        space = args.get("space", "")
        if not space:
            return {"error": "space is required"}
        page_size = min(args.get("page_size", 25), 100)
        return _run_gws(
            [
                "chat",
                "spaces",
                "messages",
                "list",
                "--params",
                _json.dumps({"parent": space, "pageSize": page_size}),
            ]
        )

    return {"error": f"Unknown gws tool: {name}"}


# Register all GWS tools as async handlers that delegate to sync _handle_gws_tool
async def _gws_handler(
    args: dict[str, Any], ctx: ToolContext, *, tool_name: str = ""
) -> dict[str, Any]:
    return await asyncio.to_thread(_handle_gws_tool, tool_name, args)


for _tool_name in (
    "gws_gmail_search",
    "gws_gmail_get",
    "gws_gmail_send",
    "gws_gmail_modify",
    "gws_calendar_list",
    "gws_calendar_create",
    "gws_calendar_delete",
    "gws_chat_send",
    "gws_chat_list_spaces",
    "gws_chat_list_messages",
):

    def _make_handler(tn: str) -> Callable[..., Any]:
        async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            return await asyncio.to_thread(_handle_gws_tool, tn, args)

        return handler

    HANDLERS[_tool_name] = _make_handler(_tool_name)
