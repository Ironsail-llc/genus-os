"""Direct Gmail transport through the host's existing Google Workspace connection.

Only a host-bound tenant may use that connection. Every API call names the fixed
mailbox; a profile read checks its identity again before a write. No write retry
is performed here. An uncertain response stays unknown until a human reconciles
an independently read, exact sent copy. Message IDs are search aids, not provider
idempotency guarantees. API acceptance and a sent copy do not prove delivery.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses

from robothor.operations.store import digest
from robothor.sales.models import Contact, Draft
from robothor.sales.providers import ProviderError, UnknownEffect

_ID = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_RFC_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>\Z")


def headers(record):
    """Reject ambiguous identity headers instead of choosing the first or last."""
    result = {}
    try:
        values = record["payload"]["headers"]
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError
        for h in values:
            name, value = h["name"].lower(), h["value"]
            if not isinstance(value, str) or len(value) > 16000:
                raise ValueError
            if name in {
                "from",
                "to",
                "cc",
                "bcc",
                "subject",
                "message-id",
                "references",
                "in-reply-to",
                "reply-to",
                "auto-submitted",
                "x-autoreply",
                "x-autorespond",
            }:
                if name in result:
                    raise ValueError
                result[name] = (
                    str(make_header(decode_header(value))) if name == "subject" else value
                )
        return result
    except (KeyError, TypeError, ValueError, AttributeError, LookupError):
        raise ProviderError("Gmail message headers require review") from None


def participants(h, sender, recipient):
    def addresses(name):
        return [Contact.email_address(v) for _, v in getaddresses([h.get(name, "")])]

    try:
        valid = (
            addresses("from") == [sender]
            and addresses("to") == [recipient]
            and not h.get("cc")
            and not h.get("bcc")
            and (not h.get("reply-to") or addresses("reply-to") == [sender])
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError("Gmail message participants require review")


def plain_text(record):
    """Read one inline UTF-8 text/plain part; attachments/HTML are not evidence."""
    parts, count = [], 0

    def visit(part, depth=0):
        nonlocal count
        count += 1
        if count > 100 or depth > 10 or not isinstance(part, dict):
            raise ValueError
        if part.get("filename"):
            return
        if part.get("mimeType") == "text/plain":
            encoded = part.get("body", {}).get("data")
            if not isinstance(encoded, str) or len(encoded) > 100000:
                raise ValueError
            parts.append(
                base64.b64decode(
                    encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
                ).decode("utf-8")
            )
        else:
            for child in part.get("parts", []):
                visit(child, depth + 1)

    try:
        visit(record["payload"])
        if len(parts) != 1 or len(parts[0]) > 50000:
            raise ValueError
        return parts[0].replace("\r\n", "\n").replace("\r", "\n")
    except (KeyError, TypeError, ValueError, binascii.Error, UnicodeError):
        raise ProviderError("Gmail plain-text content requires review") from None


class Gmail:
    """One local OAuth account, explicitly assigned to one sales tenant."""

    def __init__(self, tenant_id, *, runner=None):
        bound = os.environ.get("ROBOTHOR_SALES_GMAIL_TENANT_ID")
        mailbox = os.environ.get("ROBOTHOR_SALES_GMAIL_MAILBOX", "")
        if not tenant_id or bound != tenant_id or not mailbox:
            raise ProviderError("Gmail host tenant/mailbox binding required")
        try:
            self.mailbox = Contact.email_address(mailbox)
        except (ValueError, TypeError):
            raise ProviderError("Invalid Gmail host mailbox binding") from None
        self.tenant = tenant_id
        self.scope = hashlib.sha256(self.mailbox.encode()).hexdigest()[:24]
        if runner is None:
            from robothor.engine.tools.handlers.gws import run_gws

            runner = run_gws
        self.runner = runner

    async def _call(self, resource, *, params=None, body=None):
        args = [
            "gmail",
            "users",
            *resource,
            "--params",
            json.dumps({**(params or {}), "userId": self.mailbox}),
        ]
        if body is not None:
            args.extend(["--json", json.dumps(body)])
        error = UnknownEffect if body is not None else ProviderError
        try:
            result = await asyncio.to_thread(self.runner, args, timeout=30)
        except Exception:
            raise error("Gmail transport failed; reconcile writes before retrying") from None
        if not isinstance(result, dict) or "error" in result:
            raise error("Gmail request failed; reconcile writes before retrying")
        return result

    async def profile(self):
        result = await self._call(["getProfile"])
        address = result.get("emailAddress")
        if not isinstance(address, str) or address.lower() != self.mailbox:
            raise ProviderError("Gmail account identity changed")
        return {"mailbox": self.mailbox, "history_id": result.get("historyId")}

    def provider_id(self, raw_id):
        if not isinstance(raw_id, str) or not _ID.fullmatch(raw_id):
            raise ProviderError("Invalid Gmail resource ID")
        return f"gmail:{self.scope}:{raw_id}"

    def raw_id(self, provider_id):
        prefix = f"gmail:{self.scope}:"
        if not isinstance(provider_id, str) or not provider_id.startswith(prefix):
            raise ProviderError("Gmail message belongs to another account")
        raw = provider_id[len(prefix) :]
        self.provider_id(raw)
        return raw

    def message_id(self, action_id, payload):
        value = digest(
            {
                "tenant": self.tenant,
                "mailbox": self.mailbox,
                "action": str(action_id),
                "draft": payload,
            }
        )
        return f"<genus-{value}@{self.mailbox.split('@')[1]}>"

    async def get_message(self, raw_id):
        self.provider_id(raw_id)
        record = await self._call(["messages", "get"], params={"id": raw_id, "format": "full"})
        if record.get("id") != raw_id:
            raise ProviderError("Gmail message identity mismatch")
        self.provider_id(record.get("threadId"))
        return record

    async def get_thread(self, thread_id):
        raw_id = self.raw_id(thread_id)
        record = await self._call(["threads", "get"], params={"id": raw_id, "format": "full"})
        if record.get("id") != raw_id:
            raise ProviderError("Gmail thread identity mismatch")
        return record

    async def prepare(self, action_id, payload):
        draft = Draft.model_validate({k: payload[k] for k in Draft.model_fields if k in payload})
        if draft.sender != self.mailbox:
            raise ProviderError("Approved sender differs from Gmail host mailbox")
        await self.profile()
        mail = EmailMessage(policy=policy.SMTP)
        mail["From"], mail["To"] = draft.sender, draft.recipient
        mail["Subject"] = draft.subject
        mail["Message-ID"] = self.message_id(action_id, payload)
        result = {"mailbox": self.mailbox}
        if draft.reply_to_uuid:
            parent = await self.get_message(self.raw_id(draft.reply_to_uuid))
            h = headers(parent)
            # Replies and follow-ups share one two-party conversation. Outbound
            # anchors are allowed, but never add participants or change subject.
            sender, recipient = (
                (draft.sender, draft.recipient)
                if "SENT" in parent.get("labelIds", [])
                else (draft.recipient, draft.sender)
            )
            participants(h, sender, recipient)
            if h.get("subject") != draft.subject:
                raise ProviderError("Approved reply subject differs from Gmail thread")
            parent_id = h.get("message-id", "")
            refs = h.get("references", "").split()
            if (
                not _RFC_ID.fullmatch(parent_id)
                or len(refs) > 30
                or any(not _RFC_ID.fullmatch(x) for x in refs)
            ):
                raise ProviderError("Gmail reply headers require review")
            mail["In-Reply-To"] = parent_id
            mail["References"] = " ".join([*refs, parent_id])
            result["threadId"] = parent["threadId"]
        mail.set_content(draft.body, cte="base64")
        result["raw"] = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        return result

    def _receipt(self, result, *, status):
        try:
            return {
                "id": self.provider_id(result["id"]),
                "gmail_message_id": result["id"],
                "thread_id": self.provider_id(result["threadId"]),
                "mailbox": self.mailbox,
                "delivery_status": status,
            }
        except (KeyError, ProviderError):
            raise UnknownEffect(
                "Gmail acknowledgement incomplete; reconcile before retrying"
            ) from None

    async def send(self, prepared, *, before_send=None):
        if prepared.get("mailbox") != self.mailbox:
            raise ProviderError("Prepared Gmail account identity changed")
        await self.profile()
        if before_send is not None:
            await before_send()
        result = await self._call(
            ["messages", "send"],
            body={k: prepared[k] for k in ("raw", "threadId") if k in prepared},
        )
        if prepared.get("threadId") and result.get("threadId") != prepared["threadId"]:
            raise UnknownEffect("Gmail acknowledged a different thread; reconcile before retrying")
        return self._receipt(result, status="provider_accepted")

    async def find_sent(self, action_id, payload, *, include_record=False):
        """Read-only evidence for reconciliation; never interprets absence as failure."""
        if payload.get("sender") != self.mailbox:
            raise ProviderError("Approved sender differs from Gmail host mailbox")
        await self.profile()
        rfc_id = self.message_id(action_id, payload)
        page = await self._call(
            ["messages", "list"], params={"q": f"in:sent rfc822msgid:{rfc_id}", "maxResults": 10}
        )
        items = page.get("messages", [])
        if page.get("nextPageToken") or not isinstance(items, list) or len(items) != 1:
            raise ProviderError("Gmail reconciliation requires one unique complete search result")
        try:
            raw_id = items[0]["id"]
        except (KeyError, TypeError):
            raise ProviderError("Gmail reconciliation result identity invalid") from None
        record = await self.get_message(raw_id)
        h = headers(record)
        participants(h, payload["sender"], payload["recipient"])
        expected = payload["body"].replace("\r\n", "\n").replace("\r", "\n")
        if not expected.endswith("\n"):
            expected += "\n"
        if (
            "SENT" not in record.get("labelIds", [])
            or h.get("message-id") != rfc_id
            or h.get("subject") != payload["subject"]
            or plain_text(record) != expected
        ):
            raise ProviderError("Gmail sent content does not match the approved action")
        if payload.get("reply_to_uuid"):
            parent = await self.get_message(self.raw_id(payload["reply_to_uuid"]))
            if record["threadId"] != parent["threadId"] or h.get("in-reply-to") != headers(
                parent
            ).get("message-id"):
                raise ProviderError("Gmail sent reply identity mismatch")
        receipt = self._receipt(record, status="sent_copy_verified")
        return {"receipt": receipt, "record": record} if include_record else receipt
