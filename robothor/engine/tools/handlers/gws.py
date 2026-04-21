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


def _resolve_robothor_email() -> str:
    """Resolve the bot's own email for duplicate-reply detection.

    ROBOTHOR_AI_EMAIL is the bot's sending address (e.g. bot@example.com).
    This must NOT use owner_config/ROBOTHOR_OWNER_EMAIL — that is the operator's
    address, and stripping it from reply recipients causes "No recipients
    found in thread" errors on every thread the operator is part of.

    Callers MUST truthiness-check before substring matching —
    `"" in <anything>` is True, which silently drops every reply.
    """
    # ROBOTHOR_AI_EMAIL is the bot's email — always prefer it
    ai_email = os.environ.get("ROBOTHOR_AI_EMAIL", "").lower().strip()
    if ai_email:
        return ai_email
    # Fallback: try owner_config only if AI email not set (legacy installs)
    try:
        from robothor.owner_config import load_owner_config

        cfg = load_owner_config()
        if cfg is not None and cfg.email:
            return cfg.email.lower().strip()
    except Exception:
        pass
    return ""


ROBOTHOR_EMAIL = _resolve_robothor_email()
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

HANDLERS: dict[str, Any] = {}


# ── Contact 360 write-through helpers ────────────────────────────────────────
# After a successful gws Gmail / Calendar write, mirror it into the Contact
# 360 fabric (message + message_participant + timeline_activity, or
# calendar_event + participants). Best-effort — failure does not propagate.


def _resolve_person_by_email(email: str) -> str | None:
    """Look up person_id for an email address. Checks contact_identifiers
    across any channel (production data has operator emails on channel='api'
    and channel='email' mixed), and falls back to crm_people.email /
    additional_emails so the operator and any other person who only has
    their address on the person row still resolves."""
    try:
        from robothor.db.connection import get_connection

        addr = email.lower().strip()
        if not addr:
            return None
        with get_connection() as conn:
            cur = conn.cursor()
            # 1. contact_identifiers — any channel where identifier matches.
            cur.execute(
                """
                SELECT ci.person_id
                  FROM contact_identifiers ci
                  JOIN crm_people p ON p.id = ci.person_id
                 WHERE lower(ci.identifier) = %s
                   AND p.deleted_at IS NULL
                 LIMIT 1
                """,
                (addr,),
            )
            row = cur.fetchone()
            if row:
                pid = row[0] if not isinstance(row, dict) else row.get("person_id")
                if pid:
                    return str(pid)
            # 2. crm_people direct match (primary email + additional_emails JSONB).
            cur.execute(
                """
                SELECT id
                  FROM crm_people
                 WHERE deleted_at IS NULL
                   AND (lower(email) = %s
                        OR additional_emails::text ILIKE %s)
                 LIMIT 1
                """,
                (addr, f'%"{addr}"%'),
            )
            row = cur.fetchone()
            if row:
                pid = row[0] if not isinstance(row, dict) else row.get("id")
                return str(pid) if pid else None
            return None
    except Exception as e:  # noqa: BLE001
        logger.debug("email person resolve failed: %s", e)
        return None


def _record_sent_email(
    *,
    result: dict[str, Any],
    to: str,
    cc: str,
    subject: str,
    body: str,
    tenant_id: str | None = None,
) -> None:
    """Write message_thread + message + message_participant + timeline_activity
    for a gmail send response. Called only on success (no 'error' key).
    """
    try:
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        if not isinstance(result, dict) or "error" in result:
            return
        gmail_id = result.get("id")
        thread_id_ext = result.get("threadId") or gmail_id
        if not gmail_id or not thread_id_ext:
            return
        tenant_id = tenant_id or DEFAULT_TENANT

        recipients = [addr for addr in _EMAIL_RE.findall(to or "") if addr]
        cc_recipients = [addr for addr in _EMAIL_RE.findall(cc or "") if addr]
        if not recipients and not cc_recipients:
            return

        with get_connection() as conn:
            cur = conn.cursor()

            # 1. message_thread upsert.
            cur.execute(
                """
                INSERT INTO message_thread
                    (tenant_id, channel, external_thread_id, subject,
                     last_message_at, message_count)
                VALUES (%s, 'email', %s, %s, NOW(), 1)
                ON CONFLICT (tenant_id, channel, external_thread_id)
                DO UPDATE SET last_message_at = EXCLUDED.last_message_at,
                              message_count   = message_thread.message_count + 1,
                              updated_at      = NOW()
                RETURNING id
                """,
                (tenant_id, str(thread_id_ext), subject or None),
            )
            row = cur.fetchone()
            thread_row_id = row[0] if not isinstance(row, dict) else row["id"]

            # 2. message insert.
            snippet = (body or "")[:200]
            cur.execute(
                """
                INSERT INTO message
                    (tenant_id, thread_id, channel, direction,
                     external_message_id, subject, body_text, snippet, occurred_at)
                VALUES (%s, %s, 'email', 'outbound', %s, %s, %s, %s, NOW())
                ON CONFLICT (tenant_id, channel, external_message_id)
                DO UPDATE SET thread_id = EXCLUDED.thread_id
                RETURNING id, (xmax = 0) AS inserted
                """,
                (
                    tenant_id,
                    thread_row_id,
                    str(gmail_id),
                    subject or None,
                    body or None,
                    snippet or None,
                ),
            )
            r = cur.fetchone()
            if isinstance(r, dict):
                message_id, inserted = r["id"], r["inserted"]
            else:
                message_id, inserted = r[0], r[1]
            if not inserted:
                return  # already linked — don't duplicate participants/timeline

            # 3. participants per role. Sender is the operator (ROBOTHOR_EMAIL).
            for role, addrs in (("to", recipients), ("cc", cc_recipients)):
                for addr in addrs:
                    pid = _resolve_person_by_email(addr)
                    cur.execute(
                        """
                        INSERT INTO message_participant
                            (tenant_id, message_id, role, person_id, handle)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (tenant_id, message_id, role, pid, addr),
                    )
                    # 4. timeline_activity for resolved recipients.
                    if pid:
                        cur.execute(
                            """
                            INSERT INTO timeline_activity
                                (tenant_id, person_id, occurred_at, activity_type,
                                 source_table, source_id, channel, direction,
                                 title, snippet)
                            VALUES (%s, %s, NOW(), 'email', 'message', %s,
                                    'email', 'outbound', %s, %s)
                            ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                            """,
                            (
                                tenant_id,
                                pid,
                                str(message_id),
                                subject or None,
                                snippet or None,
                            ),
                        )

            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("gws email write-through failed: %s", e)


def _record_calendar_event(
    *,
    result: dict[str, Any],
    tenant_id: str | None = None,
) -> None:
    """Upsert calendar_event + calendar_event_participant rows from a gws
    calendar-create response. Emits timeline_activity per resolved attendee."""
    try:
        from robothor.constants import DEFAULT_TENANT
        from robothor.db.connection import get_connection

        if not isinstance(result, dict) or "error" in result:
            return
        google_id = result.get("id")
        if not google_id:
            return
        tenant_id = tenant_id or DEFAULT_TENANT

        summary = result.get("summary")
        status = result.get("status")
        html_link = result.get("htmlLink")
        description = result.get("description")
        location = result.get("location")
        start_at = (result.get("start") or {}).get("dateTime") or (result.get("start") or {}).get(
            "date"
        )
        end_at = (result.get("end") or {}).get("dateTime") or (result.get("end") or {}).get("date")
        attendees = result.get("attendees") or []
        organizer_email = (result.get("organizer") or {}).get("email")

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO calendar_event
                    (tenant_id, google_event_id, title, description, location,
                     start_at, end_at, organizer_email, hangout_link, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, google_event_id)
                DO UPDATE SET title            = EXCLUDED.title,
                              description      = EXCLUDED.description,
                              location         = EXCLUDED.location,
                              start_at         = EXCLUDED.start_at,
                              end_at           = EXCLUDED.end_at,
                              organizer_email  = EXCLUDED.organizer_email,
                              hangout_link     = EXCLUDED.hangout_link,
                              status           = EXCLUDED.status,
                              updated_at       = NOW()
                RETURNING id
                """,
                (
                    tenant_id,
                    str(google_id),
                    summary,
                    description,
                    location,
                    start_at,
                    end_at,
                    organizer_email,
                    html_link,
                    status,
                ),
            )
            row = cur.fetchone()
            event_id = row[0] if not isinstance(row, dict) else row["id"]

            # Purge previous participants for this event and re-insert (keeps
            # response_status fresh on repeat calls).
            cur.execute("DELETE FROM calendar_event_participant WHERE event_id = %s", (event_id,))

            for att in attendees:
                email = (att.get("email") or "").lower().strip()
                if not email:
                    continue
                pid = _resolve_person_by_email(email)
                is_organizer = bool(att.get("organizer"))
                cur.execute(
                    """
                    INSERT INTO calendar_event_participant
                        (tenant_id, event_id, person_id, role, email,
                         display_name, response_status, organizer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        event_id,
                        pid,
                        "organizer" if is_organizer else "attendee",
                        email,
                        att.get("displayName"),
                        att.get("responseStatus"),
                        is_organizer,
                    ),
                )
                if pid:
                    cur.execute(
                        """
                        INSERT INTO timeline_activity
                            (tenant_id, person_id, occurred_at, activity_type,
                             source_table, source_id, channel, title, snippet)
                        VALUES (%s, %s, %s, 'calendar_event', 'calendar_event', %s,
                                'calendar', %s, %s)
                        ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                        """,
                        (
                            tenant_id,
                            pid,
                            start_at,
                            str(event_id),
                            summary,
                            (description or summary or "")[:200]
                            if (description or summary)
                            else None,
                        ),
                    )

            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("gws calendar write-through failed: %s", e)


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

    Either normalized string contains the other (catches "Team Weekly" vs
    "Team Weekly Leadership"), OR normalized strings are equal.
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
        base = datetime.fromisoformat(start)
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

        # Duplicate guard: skip if last message is already from us.
        # MUST truthiness-check — empty ROBOTHOR_EMAIL is a substring of
        # every string, so a bare `in` check would drop every reply.
        last_from = last_headers.get("From", "")
        if ROBOTHOR_EMAIL and ROBOTHOR_EMAIL in last_from.lower():
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

        reply_result = _run_gws(
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
        _record_sent_email(
            result=reply_result if isinstance(reply_result, dict) else {},
            to=", ".join(to_addresses),
            cc=", ".join(cc_addresses),
            subject=subject,
            body=body,
        )
        return reply_result

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
                        if ROBOTHOR_EMAIL and ROBOTHOR_EMAIL in last_from.lower():
                            return {
                                "status": "skipped",
                                "reason": "Already replied to this thread — last message is from robothor",
                            }
            except Exception:
                logger.debug("Gmail send duplicate guard failed", exc_info=True)

        content_type = args.get("content_type", "text")
        # Defensive: if the caller forgot content_type but the body is clearly HTML
        # (starts with <!DOCTYPE or <html), treat it as HTML. Prevents agents from
        # silently sending rendered HTML as plaintext and showing raw tags.
        if content_type != "html":
            body_head = body.lstrip()[:15].lower()
            if body_head.startswith("<!doctype html") or body_head.startswith("<html"):
                logger.warning(
                    "gws_gmail_send: body looks like HTML but content_type=%r; "
                    "auto-upgrading to html. to=%s",
                    content_type,
                    to,
                )
                content_type = "html"
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

        _record_sent_email(
            result=result if isinstance(result, dict) else {},
            to=to,
            cc=cc,
            subject=subject,
            body=body,
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

        cal_result = _run_gws(
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
        _record_calendar_event(result=cal_result if isinstance(cal_result, dict) else {})
        return cal_result

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
