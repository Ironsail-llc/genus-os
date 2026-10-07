"""Exchange Online mail behind :class:`~robothor.workspace.protocols.MailProvider`.

:class:`GraphMail` serves the ``gws_gmail_*`` tools from the assistant's
Microsoft 365 mailbox (the ``m365_assistant_mailbox`` setting). The handlers
keep every guard and read Gmail's shapes, so this provider speaks Gmail on the
way out:

* **Messages come back Gmail-shaped.** A Graph message is translated into the
  raw form Gmail returns -- ``threadId`` (the ``conversationId``),
  ``labelIds``, ``snippet`` and ``payload.headers`` (``From``/``To``/``Cc``/
  ``Subject``/``Date``/``Message-ID``) with the body as a base64url part -- and
  :mod:`robothor.workspace.google.gmail_parse` shapes it. One shaper, so a tool
  result has the same keys whichever mailbox it came from, and the
  duplicate-reply and reply-all code reads headers it already understands.
* **Labels are synthesized** (:func:`labels_for`): ``UNREAD`` when unread, a
  folder label (``INBOX``/``SENT``/``TRASH``/``SPAM``/``DRAFT``), ``STARRED``
  when flagged, ``IMPORTANT`` when high importance, then the Outlook
  categories. :meth:`GraphMail.modify` maps them back.
* **Search never widens.** A Gmail query is translated to ``$filter`` when
  every term is a structured one (server-side ``receivedDateTime desc``), or to
  KQL ``$search`` when a term needs text matching, with every structured term
  also applied to each result. Anything with no faithful translation raises
  :class:`~robothor.workspace.errors.Unsupported`, naming what is supported.
  As in Gmail, Deleted Items and Junk Email are left out unless asked for.
* **Sends always yield an id.** A new message is a draft created from the
  handler's MIME, then sent; the draft's immutable id is the sent message's id.
  A send into a conversation (``reply``, or ``send`` with a thread) is
  ``createReplyAll`` on the newest message, with the recipients and body then
  set to EXACTLY what the handler built -- the do-not-contact screen approved
  those addresses, and Exchange's own reply-all list is never trusted.
* **Writes go through** :class:`~robothor.workspace.microsoft.graph.GraphClient`
  once: an unknown outcome surfaces as
  :class:`~robothor.workspace.errors.UnknownEffect` and is never retried here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import email.policy
import email.utils
import html
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from robothor.workspace.errors import NotFound, Unsupported, WorkspaceError
from robothor.workspace.google import gmail_parse
from robothor.workspace.types import AllOf, AnyOf, MailQuery, QueryNode, Term

if TYPE_CHECKING:
    from robothor.workspace.microsoft.graph import GraphClient

__all__ = [
    "SUPPORTED_LABELS",
    "SUPPORTED_SEARCH",
    "GraphMail",
    "SearchPlan",
    "labels_for",
    "translate_query",
]

#: Folders the provider addresses by Graph's well-known name.
_FOLDERS = ("inbox", "sentitems", "deleteditems", "junkemail", "drafts", "archive")
#: Gmail's folder-like system labels.
_FOLDER_LABELS = {
    "inbox": "INBOX",
    "sentitems": "SENT",
    "deleteditems": "TRASH",
    "junkemail": "SPAM",
    "drafts": "DRAFT",
}
#: ``in:`` values -> well-known folder (``None`` = every folder).
_IN_FOLDERS: dict[str, str | None] = {
    "inbox": "inbox",
    "sent": "sentitems",
    "trash": "deleteditems",
    "spam": "junkemail",
    "drafts": "drafts",
    "draft": "drafts",
    "anywhere": None,
}
#: Left out of a search that names no folder, as Gmail leaves out trash and spam.
_EXCLUDED_BY_DEFAULT = ("deleteditems", "junkemail")

SUPPORTED_SEARCH = (
    "from: to: (an address filters exactly; a name matches text), cc:, bcc:, subject:, "
    'free text and "phrases", is:unread/read/starred/important, has:attachment, '
    "label:<category>, in:inbox/sent/trash/spam/drafts/anywhere, after:/before: "
    "(YYYY/MM/DD or epoch seconds), newer_than:/older_than: (Nh/Nd/Nm/Ny), "
    "OR and {a b} groups of the same kind, and -is:/-has: negation"
)
SUPPORTED_LABELS = (
    "UNREAD (read state), STARRED (flag), IMPORTANT (importance), "
    "INBOX (remove = archive, add = move to inbox), TRASH (Deleted Items), "
    "and any other name as an Outlook category"
)

_ENVELOPE_FIELDS = (
    "id",
    "conversationId",
    "receivedDateTime",
    "sentDateTime",
    "subject",
    "bodyPreview",
    "from",
    "toRecipients",
    "ccRecipients",
    "isRead",
    "isDraft",
    "flag",
    "importance",
    "categories",
    "parentFolderId",
    "internetMessageId",
    "hasAttachments",
)
_FULL_FIELDS = (*_ENVELOPE_FIELDS, "body", "internetMessageHeaders")
_SEARCH_FIELDS = (
    "id",
    "conversationId",
    "receivedDateTime",
    "parentFolderId",
    "from",
    "toRecipients",
    "isRead",
    "flag",
    "importance",
    "hasAttachments",
    "categories",
)
_PREFER_TEXT = 'outlook.body-content-type="text"'
_EPOCH_FLOOR = "1900-01-01T00:00:00Z"
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_RELATIVE_RE = re.compile(r"^(\d+)([hdmy])$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")
_RELATIVE_UNITS = {"h": timedelta(hours=1), "d": timedelta(days=1)}
_KQL_PLAIN = re.compile(r'^[^\s():"]+$')

#: Upper bounds on what one search reads to fill ``max_results``.
_SEARCH_PAGE = 50
_SEARCH_MAX_READ = 500
_THREAD_MAX = 200

Predicate = Callable[[dict[str, Any]], bool]


# ── labels ────────────────────────────────────────────────────────────


def labels_for(message: dict[str, Any], folders: dict[str, str]) -> list[str]:
    """Gmail label ids for a Graph message. ``folders`` maps well-known name -> id."""
    labels: list[str] = []
    if not message.get("isRead", True):
        labels.append("UNREAD")
    parent = message.get("parentFolderId")
    for name, label in _FOLDER_LABELS.items():
        if parent and folders.get(name) == parent:
            labels.append(label)
    if message.get("isDraft") and "DRAFT" not in labels:
        labels.append("DRAFT")
    if (message.get("flag") or {}).get("flagStatus") == "flagged":
        labels.append("STARRED")
    if str(message.get("importance") or "").lower() == "high":
        labels.append("IMPORTANT")
    labels.extend(str(c) for c in message.get("categories") or [])
    return labels


# ── query translation ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchPlan:
    """A Gmail query as Graph parameters. Exactly one of ``filter``/``search`` style."""

    folder: str | None = None
    exclude_folders: tuple[str, ...] = _EXCLUDED_BY_DEFAULT
    filter: str | None = None
    orderby: str | None = None
    search: str | None = None
    predicates: tuple[Predicate, ...] = field(default=())


@dataclass
class _Piece:
    filter: str | None = None
    kql: str | None = None
    predicate: Predicate | None = None
    folder: str | None = None
    is_folder: bool = False
    is_date: bool = False


def _refuse(what: str) -> Unsupported:
    return Unsupported(
        f"Microsoft 365 search cannot express {what}, and will not drop it and return "
        f"more mail than asked. Supported: {SUPPORTED_SEARCH}.",
        code="unsupported_query",
    )


def _odata(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _kql_value(value: str) -> str:
    if _KQL_PLAIN.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _address_of(recipient: Any) -> str:
    return str(((recipient or {}).get("emailAddress") or {}).get("address") or "").lower()


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(value: str) -> datetime | None:
    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=UTC)
    match = _DATE_RE.match(value)
    if not match:
        return None
    try:
        return datetime(*(int(g) for g in match.groups()), tzinfo=UTC)
    except ValueError:
        return None


def _relative(value: str, now: datetime) -> datetime | None:
    match = _RELATIVE_RE.match(value)
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2).lower()
    if unit == "m":
        delta = timedelta(days=30 * count)
    elif unit == "y":
        delta = timedelta(days=365 * count)
    else:
        delta = _RELATIVE_UNITS[unit] * count
    return now - delta


def _received_at_least(stamp: str) -> Predicate:
    return lambda m: str(m.get("receivedDateTime") or "") >= stamp


def _received_before(stamp: str) -> Predicate:
    return lambda m: str(m.get("receivedDateTime") or "") < stamp


def _date_piece(op: str, when: datetime) -> _Piece:
    stamp = _iso(when)
    if op == "ge":
        return _Piece(f"receivedDateTime ge {stamp}", None, _received_at_least(stamp), is_date=True)
    return _Piece(f"receivedDateTime lt {stamp}", None, _received_before(stamp), is_date=True)


def _is_piece(value: str, negated: bool) -> _Piece | None:
    if value in ("unread", "read"):
        unread = (value == "unread") != negated
        return _Piece(
            f"isRead eq {'false' if unread else 'true'}",
            None,
            lambda m: bool(m.get("isRead")) != unread,
        )
    if value == "starred":
        op = "ne" if negated else "eq"
        return _Piece(
            f"flag/flagStatus {op} 'flagged'",
            None,
            lambda m: ((m.get("flag") or {}).get("flagStatus") == "flagged") != negated,
        )
    if value == "important":
        op = "ne" if negated else "eq"
        return _Piece(
            f"importance {op} 'high'",
            None,
            lambda m: (str(m.get("importance") or "").lower() == "high") != negated,
        )
    return None


def _term_piece(term: Term, now: datetime) -> _Piece:
    field_, value, negated = term.field, term.value, term.negated
    lowered = value.lower()

    if field_ == "label" and lowered in ("inbox", "sent", "trash", "spam", "drafts", "draft"):
        field_ = "in"
    elif field_ == "label" and lowered in ("unread", "starred", "important"):
        field_ = "is"

    if field_ == "is":
        piece = _is_piece(lowered, negated)
        if piece is None:
            raise _refuse(f"is:{value}")
        return piece
    if field_ == "has":
        if lowered not in ("attachment", "attachments"):
            raise _refuse(f"has:{value}")
        wanted = not negated
        return _Piece(
            f"hasAttachments eq {'true' if wanted else 'false'}",
            None,
            lambda m: bool(m.get("hasAttachments")) == wanted,
        )
    if negated:
        # Graph's $filter has no dependable NOT over these, and KQL's NOT is
        # fuzzy: refuse rather than approximate an exclusion.
        raise _refuse(f"the negated term -{field_}:{value}")
    if field_ == "in":
        if lowered not in _IN_FOLDERS:
            raise _refuse(f"in:{value}")
        return _Piece(folder=_IN_FOLDERS[lowered], is_folder=True)
    if field_ == "label":
        return _Piece(
            f"categories/any(c: c eq {_odata(value)})",
            None,
            lambda m: value.lower() in (str(c).lower() for c in m.get("categories") or []),
        )
    if field_ in ("after", "before"):
        when = _parse_date(value)
        if when is None:
            raise _refuse(f"{field_}:{value}")
        return _date_piece("ge" if field_ == "after" else "lt", when)
    if field_ in ("newer_than", "older_than"):
        when = _relative(value, now)
        if when is None:
            raise _refuse(f"{field_}:{value}")
        return _date_piece("ge" if field_ == "newer_than" else "lt", when)
    if field_ in ("from", "to"):
        kql = f"{field_}:{_kql_value(value)}"
        if not _ADDRESS_RE.match(value):
            return _Piece(None, kql, None)
        address = lowered
        if field_ == "from":
            return _Piece(
                f"from/emailAddress/address eq {_odata(address)}",
                kql,
                lambda m: _address_of(m.get("from")) == address,
            )
        return _Piece(
            f"toRecipients/any(r: r/emailAddress/address eq {_odata(address)})",
            kql,
            lambda m: any(_address_of(r) == address for r in m.get("toRecipients") or []),
        )
    if field_ in ("cc", "bcc", "subject"):
        return _Piece(None, f"{field_}:{_kql_value(value)}", None)
    if field_ == "text":
        return _Piece(None, _kql_value(value), None)
    if field_ == "phrase":
        return _Piece(None, '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"', None)
    raise _refuse(f"{field_}:{value}")


def _combine(parts: list[_Piece], joiner: str) -> _Piece:
    """``joiner`` is ``or`` (an AnyOf) or ``and`` (an AllOf nested in one)."""
    if any(p.is_folder for p in parts):
        raise _refuse("a folder (in:) inside a group")
    filt = (
        "(" + f" {joiner} ".join(p.filter for p in parts if p.filter) + ")"
        if all(p.filter for p in parts)
        else None
    )
    kql = (
        "(" + f" {joiner.upper()} ".join(p.kql for p in parts if p.kql) + ")"
        if all(p.kql for p in parts)
        else None
    )
    predicate: Predicate | None = None
    if all(p.predicate for p in parts):
        preds = [p.predicate for p in parts if p.predicate]
        combine = any if joiner == "or" else all
        predicate = lambda m: combine(pred(m) for pred in preds)  # noqa: E731
    if filt is None and kql is None:
        raise _refuse("a group mixing text terms with flag, date or label terms")
    return _Piece(filt, kql, predicate)


def _node_piece(node: QueryNode, now: datetime) -> _Piece:
    if isinstance(node, Term):
        return _term_piece(node, now)
    if isinstance(node, AnyOf):
        return _combine([_node_piece(p, now) for p in node.parts], "or")
    return _combine([_node_piece(p, now) for p in node.parts], "and")


def _flatten(nodes: Iterable[QueryNode]) -> list[QueryNode]:
    out: list[QueryNode] = []
    for node in nodes:
        if isinstance(node, AllOf):
            out.extend(_flatten(node.parts))
        else:
            out.append(node)
    return out


def translate_query(query: MailQuery, *, now: datetime | None = None) -> SearchPlan:
    """A parsed Gmail query as a :class:`SearchPlan`, or :class:`Unsupported`."""
    if query.raw:
        raise _refuse(" ".join(f"'{token}'" for token in query.raw))
    now = now or datetime.now(UTC)
    pieces = [_node_piece(node, now) for node in _flatten(query.clauses)]

    folders = [p for p in pieces if p.is_folder]
    if len(folders) > 1:
        raise _refuse("more than one in: folder")
    folder_named = bool(folders)
    folder = folders[0].folder if folders else None
    exclude = () if folder_named else _EXCLUDED_BY_DEFAULT
    pieces = [p for p in pieces if not p.is_folder]

    if all(p.filter for p in pieces):
        # Graph wants the $orderby property restricted first in $filter, or it
        # answers InefficientFilter; date terms lead, and a no-op floor stands
        # in when there are none.
        ordered = [p.filter for p in pieces if p.is_date] + [
            p.filter for p in pieces if not p.is_date
        ]
        clauses = [c for c in ordered if c]
        if clauses and not clauses[0].startswith("receivedDateTime "):
            clauses.insert(0, f"receivedDateTime ge {_EPOCH_FLOOR}")
        return SearchPlan(
            folder=folder,
            exclude_folders=exclude,
            filter=" and ".join(clauses) or None,
            orderby="receivedDateTime desc",
        )

    # KQL: $search cannot be combined with $filter or $orderby, so every
    # structured term is ALSO checked on each result, and the order is ours.
    kql: list[str] = []
    predicates: list[Predicate] = []
    for piece in pieces:
        if piece.kql is None and piece.predicate is None:
            raise _refuse("this combination of terms")
        if piece.kql is not None:
            kql.append(piece.kql)
        if piece.predicate is not None:
            predicates.append(piece.predicate)
    return SearchPlan(
        folder=folder,
        exclude_folders=exclude,
        search=" AND ".join(kql),
        predicates=tuple(predicates),
    )


# ── Gmail-shaped raw messages ─────────────────────────────────────────


def _format_address(recipient: Any) -> str:
    address = (recipient or {}).get("emailAddress") or {}
    addr = str(address.get("address") or "")
    name = str(address.get("name") or "")
    if not name or name.lower() == addr.lower():
        return addr
    return email.utils.formataddr((name, addr))


def _format_addresses(recipients: Any) -> str:
    return ", ".join(a for a in (_format_address(r) for r in recipients or []) if a)


def _rfc2822(stamp: str) -> str:
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return email.utils.format_datetime(when.astimezone(UTC))


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _gmailize(message: dict[str, Any], folders: dict[str, str]) -> dict[str, Any]:
    """A Graph message in the raw form Gmail's API returns (see the module doc)."""
    headers: list[dict[str, str]] = []

    def add(name: str, value: str) -> None:
        if value:
            headers.append({"name": name, "value": value})

    add("From", _format_address(message.get("from")))
    add("To", _format_addresses(message.get("toRecipients")))
    add("Cc", _format_addresses(message.get("ccRecipients")))
    add("Subject", str(message.get("subject") or ""))
    add("Date", _rfc2822(str(message.get("sentDateTime") or message.get("receivedDateTime") or "")))
    add("Message-ID", str(message.get("internetMessageId") or ""))
    present = {h["name"].lower() for h in headers} | {"from", "to", "cc", "subject", "date"}
    for header in message.get("internetMessageHeaders") or []:
        name = str((header or {}).get("name") or "")
        if name and name.lower() not in present and name.lower() != "message-id":
            headers.append({"name": name, "value": str(header.get("value") or "")})

    payload: dict[str, Any] = {"headers": headers, "body": {}}
    body = message.get("body")
    if isinstance(body, dict) and body.get("content"):
        is_html = str(body.get("contentType") or "").lower() == "html"
        payload["mimeType"] = "text/html" if is_html else "text/plain"
        payload["body"] = {"data": _b64url(str(body["content"]))}
    parts = [
        {
            "filename": str(a.get("name") or "attachment"),
            "mimeType": str(a.get("contentType") or ""),
            "body": {"size": int(a.get("size") or 0)},
        }
        for a in message.get("attachments") or []
        if isinstance(a, dict)
    ]
    if parts:
        payload["parts"] = parts

    return {
        "id": str(message.get("id") or ""),
        "threadId": str(message.get("conversationId") or ""),
        "labelIds": labels_for(message, folders),
        # Gmail's snippet is HTML-escaped text; bodyPreview is plain text.
        "snippet": html.escape(str(message.get("bodyPreview") or ""), quote=False),
        "payload": payload,
    }


# ── outgoing MIME ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Outgoing:
    mime: bytes
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    content_type: str
    body: str


def _decode_raw(raw: str) -> _Outgoing:
    try:
        mime = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError):
        raise WorkspaceError("the message to send is not valid base64url") from None
    parsed = email.message_from_bytes(mime, policy=email.policy.default)

    def addresses(header: str) -> tuple[str, ...]:
        return tuple(a for _, a in email.utils.getaddresses(parsed.get_all(header, [])) if a)

    part = parsed.get_body(preferencelist=("plain", "html"))
    content_type = "html" if part is not None and part.get_content_subtype() == "html" else "text"
    body = part.get_content() if part is not None else ""
    return _Outgoing(
        mime=mime,
        to=addresses("To"),
        cc=addresses("Cc"),
        bcc=addresses("Bcc"),
        content_type=content_type,
        body=str(body),
    )


def _recipients(addresses: Iterable[str]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in addresses]


def _as_labels(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


# ── the provider ──────────────────────────────────────────────────────


class GraphMail:
    """:class:`~robothor.workspace.protocols.MailProvider` for one Exchange mailbox."""

    def __init__(
        self,
        mailbox: str,
        *,
        graph: GraphClient | None = None,
        graph_factory: Callable[[], Awaitable[GraphClient]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        mailbox = (mailbox or "").strip()
        if not mailbox:
            raise Unsupported(
                "microsoft365 mail needs the assistant's mailbox; set m365_assistant_mailbox",
                code="not_configured",
            )
        if graph is None and graph_factory is None:
            raise ValueError("GraphMail needs a graph client or a factory for one")
        self.mailbox = mailbox
        self._base = f"/users/{quote(mailbox, safe='@')}"
        self._graph_given = graph
        self._factory = graph_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._client: GraphClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._folders: dict[str, str] | None = None

    # ── plumbing ────────────────────────────────────────────────────────

    async def _graph(self) -> GraphClient:
        if self._graph_given is not None:
            return self._graph_given
        loop = asyncio.get_running_loop()
        if self._client is not None and self._client_loop is loop:
            return self._client
        if self._lock is None or self._client_loop is not loop:
            # An asyncio.Lock and an httpx client belong to one event loop.
            self._lock = asyncio.Lock()
            self._client_loop = loop
            self._client = None
        async with self._lock:
            if self._client is None:
                assert self._factory is not None
                self._client = await self._factory()
            return self._client

    async def _folder_ids(self) -> dict[str, str]:
        if self._folders is not None:
            return self._folders
        graph = await self._graph()

        async def one(name: str) -> tuple[str, str | None]:
            try:
                found = await graph.get(f"{self._base}/mailFolders/{name}", {"$select": "id"})
            except NotFound:
                return name, None
            return name, str(found.get("id") or "") or None

        pairs = await asyncio.gather(*(one(name) for name in _FOLDERS))
        self._folders = {name: fid for name, fid in pairs if fid}
        return self._folders

    def _message_path(self, message_id: str) -> str:
        if not message_id or not message_id.strip():
            raise NotFound("empty message id")
        return f"{self._base}/messages/{quote(message_id, safe='')}"

    async def _conversation(self, conversation_id: str, fields: Iterable[str], **extra: Any):
        graph = await self._graph()
        params = {
            "$filter": f"conversationId eq {_odata(conversation_id)}",
            "$select": ",".join(fields),
            "$top": "50",
        }
        params.update(extra.pop("params", {}))
        # No $orderby: with a conversationId filter Graph answers
        # InefficientFilter. Sorted here, oldest first, as Gmail returns a thread.
        items = await graph.get_all(
            f"{self._base}/messages", params, max_items=_THREAD_MAX, **extra
        )
        if not items:
            raise NotFound(f"no conversation {conversation_id!r} in the assistant's mailbox")
        items.sort(key=lambda m: str(m.get("receivedDateTime") or m.get("sentDateTime") or ""))
        return items

    # ── reads ───────────────────────────────────────────────────────────

    async def search(self, query: MailQuery, *, max_results: int) -> dict[str, Any]:
        plan = translate_query(query, now=self._now())  # refuses before any request
        graph = await self._graph()
        folders = await self._folder_ids() if plan.exclude_folders else {}
        excluded = {folders[name] for name in plan.exclude_folders if name in folders}

        path = (
            f"{self._base}/mailFolders/{plan.folder}/messages"
            if plan.folder
            else f"{self._base}/messages"
        )
        params: dict[str, Any] = {"$select": ",".join(_SEARCH_FIELDS), "$top": str(_SEARCH_PAGE)}
        if plan.search is not None:
            params["$search"] = '"' + plan.search.replace('"', '\\"') + '"'
        else:
            if plan.filter:
                params["$filter"] = plan.filter
            if plan.orderby:
                params["$orderby"] = plan.orderby
        wanted = max(1, int(max_results))
        read_cap = min(_SEARCH_MAX_READ, max(_SEARCH_PAGE, wanted * 4))
        items = await graph.get_all(path, params, max_items=read_cap)

        kept = [
            m
            for m in items
            if m.get("parentFolderId") not in excluded and all(p(m) for p in plan.predicates)
        ]
        if plan.search is not None:
            kept.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
        kept = kept[:wanted]
        return {
            "messages": [
                {"id": str(m.get("id") or ""), "threadId": str(m.get("conversationId") or "")}
                for m in kept
            ],
            "resultSizeEstimate": len(kept),
        }

    async def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        graph = await self._graph()
        full = fmt == "full"
        params: dict[str, Any] = {"$select": ",".join(_FULL_FIELDS if full else _ENVELOPE_FIELDS)}
        headers = None
        if full:
            params["$expand"] = "attachments($select=name,contentType,size,isInline)"
            headers = {"Prefer": _PREFER_TEXT}
        message = await graph.get(self._message_path(message_id), params, headers=headers)
        return _gmailize(message, await self._folder_ids())

    async def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]:
        full = fmt == "full"
        extra: dict[str, Any] = {}
        if full:
            extra = {
                "params": {"$expand": "attachments($select=name,contentType,size,isInline)"},
                "headers": {"Prefer": _PREFER_TEXT},
            }
        items = await self._conversation(
            thread_id, _FULL_FIELDS if full else _ENVELOPE_FIELDS, **extra
        )
        folders = await self._folder_ids()
        return {"id": thread_id, "messages": [_gmailize(m, folders) for m in items]}

    # ── writes ──────────────────────────────────────────────────────────

    async def send(self, raw: str, *, thread_id: str | None = None) -> dict[str, Any]:
        outgoing = _decode_raw(raw)
        if thread_id:
            return await self._send_in_conversation(outgoing, thread_id)
        graph = await self._graph()
        draft = await graph.post(
            f"{self._base}/messages",
            content=base64.b64encode(outgoing.mime),
            headers={"Content-Type": "text/plain"},
        )
        draft_id = str(draft.get("id") or "")
        if not draft_id:
            raise WorkspaceError("graph created a draft but returned no id; nothing was sent")
        await graph.post(f"{self._message_path(draft_id)}/send")
        return self._sent(draft)

    async def reply(self, raw: str, *, thread_id: str) -> dict[str, Any]:
        return await self._send_in_conversation(_decode_raw(raw), thread_id)

    @staticmethod
    def _sent(draft: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": str(draft.get("id") or ""),
            "threadId": str(draft.get("conversationId") or ""),
            "labelIds": ["SENT"],
        }
        if draft.get("internetMessageId"):
            result["internetMessageId"] = str(draft["internetMessageId"])
        return result

    async def _send_in_conversation(self, outgoing: _Outgoing, conversation_id: str):
        if not (outgoing.to or outgoing.cc or outgoing.bcc):
            raise WorkspaceError("the message to send has no recipients")
        items = await self._conversation(conversation_id, ("id", "isDraft", "receivedDateTime"))
        sent = [m for m in items if not m.get("isDraft")]
        if not sent:
            raise NotFound(f"conversation {conversation_id!r} has no sent or received message")
        newest = str(sent[-1].get("id") or "")

        graph = await self._graph()
        # createReplyAll threads natively; its recipient list is Exchange's,
        # so it is REPLACED below with exactly the handler's approved list.
        draft = await graph.post(f"{self._message_path(newest)}/createReplyAll", {})
        draft_id = str(draft.get("id") or "")
        if not draft_id:
            raise WorkspaceError("graph created a reply draft but returned no id; nothing was sent")
        try:
            if str(draft.get("conversationId") or "") != conversation_id:
                raise WorkspaceError(
                    "graph put the reply draft in a different conversation; nothing was sent"
                )
            await graph.patch(
                self._message_path(draft_id),
                {
                    "toRecipients": _recipients(outgoing.to),
                    "ccRecipients": _recipients(outgoing.cc),
                    "bccRecipients": _recipients(outgoing.bcc),
                    "body": {"contentType": outgoing.content_type, "content": outgoing.body},
                },
            )
        except WorkspaceError:
            await self._discard(draft_id)
            raise
        await graph.post(f"{self._message_path(draft_id)}/send")
        return self._sent(draft)

    async def _discard(self, draft_id: str) -> None:
        """Best effort: an unsent draft must not linger as if it were mail."""
        with contextlib.suppress(WorkspaceError):
            await (await self._graph()).delete(self._message_path(draft_id))

    async def modify(
        self, message_id: str, *, add_labels: Any, remove_labels: Any
    ) -> dict[str, Any]:
        add, remove = _as_labels(add_labels), _as_labels(remove_labels)
        add_sys = {a.upper() for a in add}
        remove_sys = {r.upper() for r in remove}
        both = add_sys & remove_sys
        if both:
            raise Unsupported(f"labels both added and removed: {sorted(both)}")
        refused = sorted(
            label
            for label in add_sys | remove_sys
            if label in ("SENT", "DRAFT", "SPAM", "CHAT") or label.startswith("CATEGORY_")
        )
        if refused:
            raise Unsupported(
                f"Microsoft 365 cannot set or clear {refused}. Supported: {SUPPORTED_LABELS}.",
                code="unsupported_label",
            )
        if "INBOX" in add_sys and "TRASH" in add_sys:
            raise Unsupported("a message cannot be moved to both INBOX and TRASH")

        system = {"UNREAD", "STARRED", "IMPORTANT", "INBOX", "TRASH"}
        patch: dict[str, Any] = {}
        if "UNREAD" in add_sys:
            patch["isRead"] = False
        if "UNREAD" in remove_sys:
            patch["isRead"] = True
        if "STARRED" in add_sys or "STARRED" in remove_sys:
            patch["flag"] = {"flagStatus": "flagged" if "STARRED" in add_sys else "notFlagged"}
        if "IMPORTANT" in add_sys or "IMPORTANT" in remove_sys:
            patch["importance"] = "high" if "IMPORTANT" in add_sys else "normal"
        move: str | None = None
        if "TRASH" in add_sys:
            move = "deleteditems"
        elif "INBOX" in add_sys or "TRASH" in remove_sys:
            move = "inbox"
        elif "INBOX" in remove_sys:
            move = "archive"

        graph = await self._graph()
        path = self._message_path(message_id)
        add_cats = [a for a in add if a.upper() not in system]
        remove_cats = {r.lower() for r in remove if r.upper() not in system}
        if add_cats or remove_cats:
            current = await graph.get(path, {"$select": "categories"})
            categories = [
                str(c) for c in current.get("categories") or [] if str(c).lower() not in remove_cats
            ]
            for category in add_cats:
                if category.lower() not in (c.lower() for c in categories):
                    categories.append(category)
            patch["categories"] = categories

        if patch:
            await graph.patch(path, patch)
        if move is not None:
            await graph.post(f"{path}/move", {"destinationId": move})

        message = await graph.get(path, {"$select": ",".join(_ENVELOPE_FIELDS)})
        return {
            "id": str(message.get("id") or message_id),
            "threadId": str(message.get("conversationId") or ""),
            "labelIds": labels_for(message, await self._folder_ids()),
        }

    # ── shaping ─────────────────────────────────────────────────────────

    def shape_envelope(
        self, raw: dict[str, Any], *, max_header_chars: int | None = None
    ) -> dict[str, Any]:
        return gmail_parse._shape_envelope(raw, max_header_chars=max_header_chars)

    def shape_message(self, raw: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
        return gmail_parse._shape_message(raw, max_chars=max_chars)
