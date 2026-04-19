"""Google Workspace (gws CLI) tool handlers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

ROBOTHOR_EMAIL = os.environ.get("ROBOTHOR_AI_EMAIL", "")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

HANDLERS: dict[str, Any] = {}


def _resolve_owner_email() -> str:
    """Operator email from ``.robothor/owner.yaml``, env-var fallback."""
    try:
        from robothor.owner_config import load_owner_config

        cfg = load_owner_config()
        if cfg is not None and cfg.email:
            return cfg.email.lower()
    except Exception:
        logger.debug("owner_config unavailable; using env fallback", exc_info=True)
    return os.environ.get("ROBOTHOR_OWNER_EMAIL", "").strip().lower()


def _normalize_summary(s: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for loose title matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip().lower()


def _summaries_match(a: str, b: str) -> bool:
    """True when two meeting titles likely name the same series.

    Either normalized string contains the other (catches "Weekly Sync" vs
    "Weekly Sync Leadership"), OR normalized strings are equal.
    """
    na, nb = _normalize_summary(a), _normalize_summary(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na in nb or nb in na


def _attendee_set(event_like: Any) -> set[str]:
    """Extract attendee emails (lowercased) from a create-payload list or a gws event dict."""
    out: set[str] = set()
    if isinstance(event_like, list):
        for entry in event_like:
            if isinstance(entry, str):
                out.add(entry.strip().lower())
            elif isinstance(entry, dict) and entry.get("email"):
                out.add(str(entry["email"]).strip().lower())
    elif isinstance(event_like, dict):
        for entry in event_like.get("attendees", []) or []:
            if isinstance(entry, dict) and entry.get("email"):
                out.add(str(entry["email"]).strip().lower())
    return out


def _attendees_overlap(proposed: set[str], existing: set[str], owner_email: str) -> bool:
    """Overlap rule for dedup.

    Ignore the operator's own email on both sides (it's auto-added, not a signal).
    Match if the smaller side has at least half its attendees in the other side,
    OR absolute overlap is at least 2.
    """
    a = {e for e in proposed if e and e != owner_email}
    b = {e for e in existing if e and e != owner_email}
    if not a or not b:
        return False
    inter = a & b
    if len(inter) >= 2:
        return True
    smaller = min(len(a), len(b))
    return smaller > 0 and (len(inter) / smaller) >= 0.5


def _find_duplicate_event(
    summary: str,
    start: str,
    attendees: list[str],
    calendar_id: str,
    owner_email: str,
    window_days: int = 14,
) -> dict[str, Any] | None:
    """Return an existing event dict if one in the ±window overlaps this proposal, else None.

    Only dedups against events with same-or-substring summary AND attendee overlap
    (per _attendees_overlap). Silent on any list failure — dedup is best-effort.
    """
    import json as _json
    from datetime import datetime, timedelta

    try:
        base = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("gws_calendar_create dedup: unparseable start=%s — skipping check", start)
        return None

    time_min = (base - timedelta(days=window_days)).isoformat()
    time_max = (base + timedelta(days=window_days)).isoformat()

    cal_params = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 250,
    }
    listed = _run_gws(["calendar", "events", "list", "--params", _json.dumps(cal_params)])
    if not isinstance(listed, dict) or "error" in listed:
        logger.debug("gws_calendar_create dedup: list failed — skipping (result=%s)", listed)
        return None

    proposed_attendees = _attendee_set(attendees)
    for event in listed.get("items", []) or []:
        if not isinstance(event, dict):
            continue
        if event.get("status") == "cancelled":
            continue
        if not _summaries_match(summary, event.get("summary", "") or ""):
            continue
        existing_attendees = _attendee_set(event)
        if not _attendees_overlap(proposed_attendees, existing_attendees, owner_email):
            continue
        return event
    return None


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

    if name == "gws_gmail_reply":
        import base64
        from email.mime.text import MIMEText

        thread_id = args.get("thread_id", "")
        body = args.get("body", "")
        extra_cc = args.get("cc", "")

        if not thread_id:
            return {"error": "thread_id is required — get it from the task body"}
        if not body:
            return {"error": "body is required"}

        # Fetch thread to extract headers, recipients, and Message-ID
        fetch_params = {
            "userId": "me",
            "id": thread_id,
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Cc", "Subject", "Message-ID"],
        }
        thread_data = _run_gws(
            ["gmail", "users", "threads", "get", "--params", _json.dumps(fetch_params)]
        )
        if isinstance(thread_data, str):
            try:
                thread_data = _json.loads(thread_data)
            except _json.JSONDecodeError:
                return {"error": f"Failed to parse thread data: {thread_data[:200]}"}
        if isinstance(thread_data, dict) and "error" in thread_data:
            return thread_data

        messages = thread_data.get("messages", []) if isinstance(thread_data, dict) else []
        if not messages:
            return {"error": f"Thread {thread_id} has no messages"}

        last_msg = messages[-1]
        last_headers: dict[str, str] = {}
        for h in last_msg.get("payload", {}).get("headers", []):
            last_headers[h["name"]] = h["value"]

        # Duplicate guard: skip if last message is already from us
        last_from = last_headers.get("From", "")
        if ROBOTHOR_EMAIL in last_from.lower():
            return {
                "status": "skipped",
                "reason": "Already replied to this thread — last message is from robothor",
            }

        # Extract Message-ID for In-Reply-To / References
        message_id_header = last_headers.get("Message-ID", "")

        # Extract subject (auto-prefix Re: if needed)
        original_subject = last_headers.get("Subject", "")
        if original_subject.lower().startswith("re:"):
            subject = original_subject
        else:
            subject = f"Re: {original_subject}"

        # Collect all recipients from entire thread (reply-all)
        all_addresses: set[str] = set()
        for m in messages:
            for h in m.get("payload", {}).get("headers", []):
                if h["name"] in ("From", "To", "Cc"):
                    all_addresses.update(a.lower() for a in _EMAIL_RE.findall(h["value"]))

        # Remove our own address from recipients
        all_addresses.discard(ROBOTHOR_EMAIL)

        # Add any extra CC from args
        extra_addrs: set[str] = set()
        if extra_cc:
            extra_addrs.update(a.lower() for a in _EMAIL_RE.findall(extra_cc))
            extra_addrs.discard(ROBOTHOR_EMAIL)

        to_addresses = sorted(all_addresses)
        cc_addresses = sorted(extra_addrs - all_addresses)

        if not to_addresses and not cc_addresses:
            return {"error": "No recipients found in thread"}

        # Build MIME message with proper threading headers
        msg = MIMEText(body)
        msg["To"] = ", ".join(to_addresses)
        msg["Subject"] = subject
        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)
        if message_id_header:
            msg["In-Reply-To"] = message_id_header
            msg["References"] = message_id_header

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        reply_json: dict[str, Any] = {"raw": raw, "threadId": thread_id}

        return _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                '{"userId":"me"}',
                "--json",
                _json.dumps(reply_json),
            ],
            timeout=30,
        )

    if name == "gws_gmail_send":
        import base64
        from email.mime.text import MIMEText

        to = args.get("to", "")
        subject = args.get("subject", "")
        body = args.get("body", "")
        cc = args.get("cc", "")
        thread_id = args.get("thread_id")
        in_reply_to = args.get("in_reply_to", "")

        # Warn if this looks like a reply but has no thread_id
        if not thread_id and subject.lower().startswith("re:"):
            logger.warning(
                "gws_gmail_send: subject starts with 'Re:' but no thread_id provided — "
                "this will create a new thread. Use gws_gmail_reply instead. to=%s subject=%s",
                to,
                subject,
            )

        # Guard: prevent duplicate replies to the same thread
        if thread_id:
            try:
                check_params = {
                    "userId": "me",
                    "id": thread_id,
                    "format": "metadata",
                    "metadataHeaders": ["From"],
                }
                thread_data = _run_gws(
                    [
                        "gmail",
                        "users",
                        "threads",
                        "get",
                        "--params",
                        _json.dumps(check_params),
                    ]
                )
                if isinstance(thread_data, str):
                    thread_data = _json.loads(thread_data)
                if isinstance(thread_data, dict):
                    messages = thread_data.get("messages", [])
                    if messages:
                        last_msg = messages[-1]
                        headers = {
                            h["name"]: h["value"]
                            for h in last_msg.get("payload", {}).get("headers", [])
                            if h.get("name") == "From"
                        }
                        last_from = headers.get("From", "")
                        if ROBOTHOR_EMAIL in last_from.lower():
                            return {
                                "status": "skipped",
                                "reason": "Already replied to this thread — last message is from robothor",
                            }
            except Exception:
                logger.debug("Gmail send duplicate guard failed", exc_info=True)

        content_type = args.get("content_type", "text")
        subtype = "html" if content_type == "html" else "plain"
        msg = MIMEText(body, _subtype=subtype)
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        send_json: dict[str, Any] = {"raw": raw}
        if thread_id:
            send_json["threadId"] = thread_id

        result = _run_gws(
            [
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                '{"userId":"me"}',
                "--json",
                _json.dumps(send_json),
            ],
            timeout=30,
        )

        # Add warning to result if threading was likely intended but missing
        if (
            not thread_id
            and subject.lower().startswith("re:")
            and isinstance(result, dict)
            and "error" not in result
        ):
            result["_warning"] = (
                "No thread_id was provided but subject starts with 'Re:'. "
                "This message was sent as a new thread, not a reply. "
                "Use gws_gmail_reply to reply within existing conversations."
            )

        return result

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

        calendar_id = args.get("calendar_id", "primary")
        owner_email = _resolve_owner_email()

        if not args.get("force"):
            dup = _find_duplicate_event(
                summary=summary,
                start=start,
                attendees=args.get("attendees", []) or [],
                calendar_id=calendar_id,
                owner_email=owner_email,
            )
            if dup is not None:
                existing_start = (dup.get("start") or {}).get("dateTime") or (
                    dup.get("start") or {}
                ).get("date", "")
                logger.warning(
                    "gws_calendar_create deduped against existing event %s "
                    "(summary=%r start=%s) — use force=true to override",
                    dup.get("id"),
                    dup.get("summary"),
                    existing_start,
                )
                return {
                    "status": "deduped",
                    "existing_event_id": dup.get("id"),
                    "summary": dup.get("summary"),
                    "start": existing_start,
                    "htmlLink": dup.get("htmlLink"),
                    "reason": (
                        "An event with a matching title and overlapping attendees "
                        "already exists within ±14 days. Not creating a duplicate. "
                        "Pass force=true to bypass this check."
                    ),
                }

        event_body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if args.get("description"):
            event_body["description"] = args["description"]
        if args.get("location"):
            event_body["location"] = args["location"]
        attendees = [{"email": e} for e in args.get("attendees", [])]
        if owner_email and not any(a["email"].lower() == owner_email for a in attendees):
            attendees.append({"email": owner_email})
        event_body["attendees"] = attendees

        with_meet = args.get("with_meet", True)
        if with_meet:
            request_id = f"robothor-{summary[:20]}-{start[:10]}".replace(" ", "-")
            event_body["conferenceData"] = {
                "createRequest": {
                    "requestId": request_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        cal_params = {"calendarId": calendar_id}
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
    "gws_gmail_reply",
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
