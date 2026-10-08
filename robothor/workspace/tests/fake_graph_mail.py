"""Exchange Online mail behind :class:`~robothor.workspace.tests.fake_graph.FakeGraphTenant`.

:func:`install_mail` registers mail routes into the tenant's handler registry
(they win over the built-in generic message routes) and returns a
:class:`FakeExchangeMail` for seeding and inspecting state. The model is the
part of Exchange the mail provider relies on, and the rules it would trip
over in production:

* **folders** by well-known name (``inbox``, ``archive``, ``deleteditems``,
  ``drafts``, ``sentitems``, ``junkemail``), each with an opaque id;
* **messages** with immutable ids carrying Graph's id alphabet
  (``A-Za-z0-9_-=+/``), a ``conversationId`` shared across a conversation;
* **drafts**: ``POST /messages`` (JSON, or base64 MIME as ``text/plain``)
  creates one in Drafts; ``POST .../send`` moves it to Sent Items KEEPING its
  id and answers 202 with no body; ``createReplyAll`` makes a draft in the
  same conversation addressed to everyone but the mailbox itself;
* **PATCH** of recipients or body is allowed on drafts only;
* **$filter / $search subset**: the OData filter grammar the provider emits
  (``eq ne ge gt le lt``, ``and``/``or``, parentheses, ``x/any(r: ...)``) and
  a small KQL (``field:value``, phrases, ``AND``/``OR``). ``$search`` with
  ``$filter`` or ``$orderby`` is a 400, as in Graph; an ``$orderby`` property
  that is not the first one the ``$filter`` restricts is a 400
  ``InefficientFilter`` -- the error ``conversationId eq ...`` plus an
  ``$orderby`` produces in production.

Bodies are stored as given; a ``Prefer: outlook.body-content-type="text"``
read returns an HTML body as crude text, as Exchange does.
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx

from robothor.workspace.tests.fake_graph import (
    GRAPH_ORIGIN,
    FakeGraphTenant,
    FakeMailbox,
    graph_error,
)

__all__ = ["WELL_KNOWN_FOLDERS", "FakeExchangeMail", "install_mail"]

WELL_KNOWN_FOLDERS = ("inbox", "archive", "deleteditems", "drafts", "sentitems", "junkemail")

#: The id alphabet Graph's immutable ids use. Every fake id carries the
#: characters a naive ``[A-Za-z0-9_-]+`` would cut off.
_ID_PREFIX = "AAkALgAA-Mx_"


def _not_found() -> httpx.Response:
    return graph_error(404, "ErrorItemNotFound", "The specified object was not found in the store.")


def _addr(address: str, name: str = "") -> dict[str, Any]:
    return {"emailAddress": {"name": name or address, "address": address}}


@dataclass
class FakeExchangeMail:
    """Seeding and inspection for the mail half of a fake tenant."""

    tenant: FakeGraphTenant
    clock: datetime = field(default_factory=lambda: datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    # ── ids and folders ─────────────────────────────────────────────────

    def new_id(self, kind: str = "msg") -> str:
        n = next(self._ids)
        return f"{_ID_PREFIX}{kind}{n:04d}+Zz/q=="

    def box(self, address: str) -> FakeMailbox:
        box = self.tenant.mailbox(address)
        if not box.collections["mailFolders"]:
            for name in WELL_KNOWN_FOLDERS:
                box.collections["mailFolders"].append(
                    {"id": f"AAMkFolder-{name}-{box.address.split('@')[0]}==", "wellKnown": name}
                )
        return box

    def folder_id(self, address: str, name: str) -> str:
        for folder in self.box(address).collections["mailFolders"]:
            if folder["wellKnown"] == name or folder["id"] == name:
                return str(folder["id"])
        raise KeyError(name)

    def folder_of(self, address: str, message: dict[str, Any]) -> str:
        for folder in self.box(address).collections["mailFolders"]:
            if folder["id"] == message.get("parentFolderId"):
                return str(folder["wellKnown"])
        return ""

    def tick(self, minutes: int = 1) -> str:
        self.clock += timedelta(minutes=minutes)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── seeding ─────────────────────────────────────────────────────────

    def deliver(
        self,
        mailbox: str,
        *,
        sender: str,
        to: list[str] | tuple[str, ...] = (),
        cc: list[str] | tuple[str, ...] = (),
        subject: str = "Hello",
        body: str = "Hi there",
        content_type: str = "text",
        folder: str = "inbox",
        conversation_id: str | None = None,
        is_read: bool = False,
        flagged: bool = False,
        importance: str = "normal",
        categories: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        sender_name: str = "",
        received: str | None = None,
        is_draft: bool = False,
    ) -> dict[str, Any]:
        """Put one message into ``mailbox`` and return it."""
        box = self.box(mailbox)
        when = received or self.tick()
        message: dict[str, Any] = {
            "id": self.new_id(),
            "conversationId": conversation_id or self.new_id("conv"),
            "parentFolderId": self.folder_id(mailbox, folder),
            "subject": subject,
            "bodyPreview": _text_of(body, content_type)[:255],
            "body": {"contentType": content_type, "content": body},
            "from": _addr(sender, sender_name),
            "sender": _addr(sender, sender_name),
            "toRecipients": [_addr(a) for a in to],
            "ccRecipients": [_addr(a) for a in cc],
            "bccRecipients": [],
            "receivedDateTime": when,
            "sentDateTime": when,
            "isRead": is_read,
            "isDraft": is_draft,
            "flag": {"flagStatus": "flagged" if flagged else "notFlagged"},
            "importance": importance,
            "categories": list(categories or []),
            "internetMessageId": f"<{self.new_id('imid')}@example.com>",
            "internetMessageHeaders": [
                {"name": "X-Mailer", "value": "FakeExchange"},
                {"name": "Date", "value": "should-not-override"},
            ],
            "hasAttachments": bool(attachments),
            "attachments": [dict(a) for a in attachments or []],
        }
        box.messages.append(message)
        return message

    def message(self, mailbox: str, message_id: str) -> dict[str, Any]:
        for message in self.box(mailbox).messages:
            if message["id"] == message_id:
                return message
        raise KeyError(message_id)

    def conversation(self, mailbox: str, conversation_id: str) -> list[dict[str, Any]]:
        return [m for m in self.box(mailbox).messages if m["conversationId"] == conversation_id]

    def writes(self) -> list[httpx.Request]:
        """Every Graph request that could change state."""
        return [r for r in self.tenant.requests if r.method != "GET"]


def _text_of(content: str, content_type: str) -> str:
    if content_type.lower() == "html":
        return " ".join(re.sub(r"<[^>]+>", " ", content).split())
    return content


# ── $filter ───────────────────────────────────────────────────────────

_FILTER_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<lp>\()|(?P<rp>\))|(?P<colon>:)|(?P<comma>,)"
    r"|(?P<word>[A-Za-z0-9_./\-+]+(?::[0-9]{2}(?::[0-9]{2})?Z?)?))"
)
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")


class _FilterError(Exception):
    pass


def _filter_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _FILTER_TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise _FilterError(f"bad filter at {text[pos:]!r}")
        tokens.append(match.group().strip())
        pos = match.end()
    return tokens


def _path_get(item: Any, path: str) -> Any:
    for part in path.split("/"):
        if not isinstance(item, dict):
            return None
        item = item.get(part)
    return item


def _literal(token: str) -> Any:
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    if token in ("true", "false"):
        return token == "true"
    if token == "null":
        return None
    if _DATETIME.match(token):
        return token if token.count(":") == 2 else token.replace("Z", ":00Z")
    try:
        return int(token)
    except ValueError:
        raise _FilterError(f"unknown literal {token!r}") from None


def _compare(left: Any, op: str, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        left, right = left.lower(), right.lower()
    if op == "eq":
        return bool(left == right)
    if op == "ne":
        return bool(left != right)
    if left is None or right is None:
        return False
    return {
        "ge": left >= right,
        "gt": left > right,
        "le": left <= right,
        "lt": left < right,
    }[op]


class _Filter:
    """A tiny OData ``$filter`` evaluator; also records which paths it restricts."""

    def __init__(self, text: str) -> None:
        self.tokens = _filter_tokens(text)
        self.pos = 0
        self.paths: list[str] = []
        self.tree = self._or()
        if self.pos != len(self.tokens):
            raise _FilterError("trailing tokens")

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise _FilterError(f"expected {expected!r}, got {token!r}")
        self.pos += 1
        return token

    def _or(self) -> Any:
        parts = [self._and()]
        while self._peek() == "or":
            self._take()
            parts.append(self._and())
        return ("or", parts) if len(parts) > 1 else parts[0]

    def _and(self) -> Any:
        parts = [self._atom()]
        while self._peek() == "and":
            self._take()
            parts.append(self._atom())
        return ("and", parts) if len(parts) > 1 else parts[0]

    def _atom(self) -> Any:
        token = self._take()
        if token == "(":
            inner = self._or()
            self._take(")")
            return inner
        if token == "not":
            return ("not", self._atom())
        if token.endswith("/any"):
            collection = token[: -len("/any")]
            self.paths.append(collection)
            self._take("(")
            var = self._take()
            self._take(":")
            inner_path = self._take()
            op = self._take()
            value = _literal(self._take())
            self._take(")")
            sub = inner_path[len(var) + 1 :] if inner_path != var else ""
            return ("any", collection, sub, op, value)
        self.paths.append(token)
        op = self._take()
        if op not in ("eq", "ne", "ge", "gt", "le", "lt"):
            raise _FilterError(f"unknown operator {op!r}")
        return ("cmp", token, op, _literal(self._take()))

    def matches(self, item: dict[str, Any], node: Any = None) -> bool:
        node = self.tree if node is None else node
        kind = node[0]
        if kind == "and":
            return all(self.matches(item, n) for n in node[1])
        if kind == "or":
            return any(self.matches(item, n) for n in node[1])
        if kind == "not":
            return not self.matches(item, node[1])
        if kind == "any":
            _, collection, sub, op, value = node
            values = _path_get(item, collection) or []
            return any(_compare(_path_get(v, sub) if sub else v, op, value) for v in values)
        _, path, op, value = node
        return _compare(_path_get(item, path), op, value)


# ── $search (KQL subset) ──────────────────────────────────────────────

_KQL_TOKEN = re.compile(r'\s*(\(|\)|[A-Za-z]+:"(?:[^"\\]|\\.)*"|"(?:[^"\\]|\\.)*"|[^\s()]+)')
_KQL_FIELDS = {
    "subject": lambda m: [m.get("subject") or ""],
    "body": lambda m: [_text_of(m["body"]["content"], m["body"]["contentType"])],
    "from": lambda m: _people(m.get("from")),
    "to": lambda m: [p for r in m.get("toRecipients") or [] for p in _people(r)],
    "cc": lambda m: [p for r in m.get("ccRecipients") or [] for p in _people(r)],
    "bcc": lambda m: [p for r in m.get("bccRecipients") or [] for p in _people(r)],
}


def _people(recipient: Any) -> list[str]:
    address = (recipient or {}).get("emailAddress") or {}
    return [str(address.get("address") or ""), str(address.get("name") or "")]


def _kql_value(raw: str) -> str:
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1]
    return raw.replace('\\"', '"').replace("\\\\", "\\")


class _Kql:
    def __init__(self, text: str) -> None:
        self.tokens = [t for t in _KQL_TOKEN.findall(text) if t]
        self.pos = 0
        self.tree = self._or()

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _or(self) -> Any:
        parts = [self._and()]
        while self._peek() == "OR":
            self.pos += 1
            parts.append(self._and())
        return ("or", parts)

    def _and(self) -> Any:
        parts = []
        while self._peek() not in (None, ")", "OR"):
            if self._peek() == "AND":
                self.pos += 1
                continue
            parts.append(self._atom())
        return ("and", parts)

    def _atom(self) -> Any:
        token = self.tokens[self.pos]
        self.pos += 1
        if token == "(":
            inner = self._or()
            if self._peek() == ")":
                self.pos += 1
            return inner
        if token == "NOT":
            return ("not", self._atom())
        name, sep, value = token.partition(":")
        if sep and name.lower() in _KQL_FIELDS and not token.startswith('"'):
            return ("field", name.lower(), _kql_value(value).lower())
        return ("text", _kql_value(token).lower())

    def matches(self, message: dict[str, Any], node: Any = None) -> bool:
        node = self.tree if node is None else node
        kind = node[0]
        if kind == "and":
            return all(self.matches(message, n) for n in node[1])
        if kind == "or":
            return any(self.matches(message, n) for n in node[1])
        if kind == "not":
            return not self.matches(message, node[1])
        if kind == "field":
            return any(node[2] in v.lower() for v in _KQL_FIELDS[node[1]](message))
        haystack = [v for get in _KQL_FIELDS.values() for v in get(message)]
        return any(node[1] in v.lower() for v in haystack)


# ── the routes ────────────────────────────────────────────────────────


def install_mail(tenant: FakeGraphTenant) -> FakeExchangeMail:
    """Register the mail routes on ``tenant``; return the state helper."""
    state = FakeExchangeMail(tenant)
    mailbox_rx = r"/users/(?P<mailbox>[^/]+)"
    message_rx = mailbox_rx + r"/messages/(?P<id>[^/]+)"

    def address(match: re.Match[str]) -> str:
        return unquote(match["mailbox"]).lower()

    def find(match: re.Match[str]) -> dict[str, Any] | None:
        target = unquote(match["id"])
        for message in state.box(address(match)).messages:
            if message["id"] == target:
                return message
        return None

    def prefers_text(request: httpx.Request) -> bool:
        return 'outlook.body-content-type="text"' in request.headers.get("prefer", "")

    def present(message: dict[str, Any], request: httpx.Request) -> dict[str, Any]:
        out = json.loads(json.dumps(message))
        if prefers_text(request) and out["body"]["contentType"] == "html":
            out["body"] = {
                "contentType": "text",
                "content": _text_of(out["body"]["content"], "html"),
            }
        expand = request.url.params.get("$expand", "")
        if "attachments" not in expand:
            out.pop("attachments", None)
        select = request.url.params.get("$select")
        if select:
            keep = {s.strip() for s in select.split(",")} | {"id"}
            if "attachments" in expand:
                keep.add("attachments")
            out = {k: v for k, v in out.items() if k in keep}
        else:
            out.pop("internetMessageHeaders", None)  # only returned when selected
        out["@odata.etag"] = f'W/"{message["id"][-8:]}"'
        return out

    async def list_in(tenant_: FakeGraphTenant, request: httpx.Request, match, folder=None):
        box = state.box(address(match))
        params = request.url.params
        flt, search, orderby = params.get("$filter"), params.get("$search"), params.get("$orderby")
        if search is not None and (flt is not None or orderby is not None):
            return graph_error(
                400,
                "ErrorInvalidUrlQuery",
                "$search cannot be combined with $filter or $orderby.",
            )
        items = list(box.messages)
        if folder is not None:
            try:
                folder_id = state.folder_id(box.address, unquote(folder))
            except KeyError:
                return _not_found()
            items = [m for m in items if m["parentFolderId"] == folder_id]
        if flt is not None:
            try:
                parsed = _Filter(flt)
            except _FilterError as exc:
                return graph_error(400, "BadRequest", f"Invalid filter clause: {exc}")
            if orderby:
                order_field = orderby.split()[0]
                if not parsed.paths or parsed.paths[0] != order_field:
                    return graph_error(
                        400,
                        "InefficientFilter",
                        "The restriction or sort order is too complex for this operation.",
                    )
            items = [m for m in items if parsed.matches(m)]
        if search is not None:
            kql = _Kql(_kql_value(search))
            items = [m for m in items if kql.matches(m)]
        if orderby:
            order_field, _, direction = orderby.partition(" ")
            items.sort(key=lambda m: str(m.get(order_field) or ""), reverse=direction == "desc")
        else:
            # No order promised: newest-inserted first, so a caller that needs
            # oldest-first must sort for itself.
            items.reverse()
        top = int(params.get("$top", "10"))
        skip = int(params.get("$skip", "0"))
        page = items[skip : skip + top]
        body: dict[str, Any] = {"value": [present(m, request) for m in page]}
        if skip + top < len(items):
            nxt = request.url.copy_merge_params({"$skip": str(skip + top)})
            body["@odata.nextLink"] = f"{GRAPH_ORIGIN}{nxt.raw_path.decode()}"
        return httpx.Response(200, json=body)

    @tenant.route("GET", mailbox_rx + r"/messages")
    async def list_messages(tenant_, request, match):
        return await list_in(tenant_, request, match)

    @tenant.route("GET", mailbox_rx + r"/mailFolders/(?P<folder>[^/]+)/messages")
    async def list_folder_messages(tenant_, request, match):
        return await list_in(tenant_, request, match, folder=match["folder"])

    @tenant.route("GET", mailbox_rx + r"/mailFolders/(?P<folder>[^/]+)")
    async def get_folder(tenant_, request, match):
        try:
            folder_id = state.folder_id(address(match), unquote(match["folder"]))
        except KeyError:
            return _not_found()
        return httpx.Response(200, json={"id": folder_id, "displayName": match["folder"]})

    @tenant.route("GET", message_rx)
    async def get_message(tenant_, request, match):
        message = find(match)
        if message is None:
            return _not_found()
        return httpx.Response(200, json=present(message, request))

    @tenant.route("POST", mailbox_rx + r"/messages")
    async def create_draft(tenant_, request, match):
        mailbox = address(match)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("text/plain"):
            try:
                mime = base64.b64decode(request.content, validate=True)
            except ValueError:
                return graph_error(400, "ErrorMimeContentInvalidBase64String", "bad base64")
            parsed = email.message_from_bytes(mime, policy=email.policy.default)
            body_part = parsed.get_body(preferencelist=("plain", "html"))
            body_type = (
                "html" if body_part and body_part.get_content_subtype() == "html" else "text"
            )
            body = body_part.get_content() if body_part else ""
            fields = {
                "subject": str(parsed.get("Subject", "")),
                "to": [a for _, a in email.utils.getaddresses(parsed.get_all("To", []))],
                "cc": [a for _, a in email.utils.getaddresses(parsed.get_all("Cc", []))],
            }
        else:
            payload = json.loads(request.content or b"{}")
            body_type = (payload.get("body") or {}).get("contentType", "text").lower()
            body = (payload.get("body") or {}).get("content", "")
            fields = {
                "subject": payload.get("subject", ""),
                "to": [r["emailAddress"]["address"] for r in payload.get("toRecipients", [])],
                "cc": [r["emailAddress"]["address"] for r in payload.get("ccRecipients", [])],
            }
        draft = state.deliver(
            mailbox,
            sender=mailbox,
            to=fields["to"],
            cc=fields["cc"],
            subject=fields["subject"],
            body=body,
            content_type=body_type,
            folder="drafts",
            is_read=True,
            is_draft=True,
        )
        return httpx.Response(201, json=present(draft, request))

    @tenant.route("POST", message_rx + r"/createReplyAll")
    async def create_reply_all(tenant_, request, match):
        original = find(match)
        if original is None:
            return _not_found()
        mailbox = address(match)
        people: list[str] = []
        for recipient in [original["from"], *original["toRecipients"]]:
            addr = recipient["emailAddress"]["address"].lower()
            if addr != mailbox and addr not in people:
                people.append(addr)
        cc = [
            r["emailAddress"]["address"].lower()
            for r in original["ccRecipients"]
            if r["emailAddress"]["address"].lower() not in {mailbox, *people}
        ]
        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"RE: {subject}"
        draft = state.deliver(
            mailbox,
            sender=mailbox,
            to=people,
            cc=cc,
            subject=subject,
            body=f"<p></p><hr>{original['body']['content']}",
            content_type="html",
            folder="drafts",
            conversation_id=original["conversationId"],
            is_read=True,
            is_draft=True,
        )
        return httpx.Response(201, json=present(draft, request))

    @tenant.route("POST", message_rx + r"/send")
    async def send_draft(tenant_, request, match):
        message = find(match)
        if message is None:
            return _not_found()
        if not message["isDraft"]:
            return graph_error(400, "ErrorInvalidOperation", "Only drafts can be sent.")
        message["isDraft"] = False
        message["parentFolderId"] = state.folder_id(address(match), "sentitems")
        message["sentDateTime"] = message["receivedDateTime"] = state.tick()
        return httpx.Response(202)

    @tenant.route("POST", message_rx + r"/move")
    async def move(tenant_, request, match):
        message = find(match)
        if message is None:
            return _not_found()
        destination = json.loads(request.content or b"{}").get("destinationId", "")
        try:
            message["parentFolderId"] = state.folder_id(address(match), destination)
        except KeyError:
            return graph_error(400, "ErrorInvalidIdMalformed", "Id is malformed.")
        # Immutable ids survive a move: the same message, the same id.
        return httpx.Response(201, json=present(message, request))

    @tenant.route("PATCH", message_rx)
    async def update(tenant_, request, match):
        message = find(match)
        if message is None:
            return _not_found()
        patch = json.loads(request.content or b"{}")
        draft_only = {"toRecipients", "ccRecipients", "bccRecipients", "body", "subject"}
        if not message["isDraft"] and draft_only & set(patch):
            return graph_error(
                400, "ErrorInvalidPropertyUpdateSentMessage", "Only drafts can be edited."
            )
        allowed = draft_only | {"isRead", "flag", "importance", "categories"}
        unknown = set(patch) - allowed
        if unknown:
            return graph_error(400, "ErrorInvalidProperty", f"Cannot set {sorted(unknown)}.")
        message.update(patch)
        if "body" in patch:
            message["body"] = {
                "contentType": str(patch["body"].get("contentType", "text")).lower(),
                "content": patch["body"].get("content", ""),
            }
            message["bodyPreview"] = _text_of(
                message["body"]["content"], message["body"]["contentType"]
            )[:255]
        return httpx.Response(200, json=present(message, request))

    return state
