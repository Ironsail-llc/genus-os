"""Gmail API message -> the flat fields a ``gws_gmail_*`` tool result carries.

Pure functions over the Gmail API's MIME tree: no CLI, no network, no state.
Moved here verbatim from ``robothor/engine/tools/handlers/gws.py`` when the
Workspace tools went behind a provider seam; that module re-exports every name
(``autonomy/mailbox.py`` and ``autonomy/verification.py`` import some of them
from there), and the Google mail adapter's shapers are these functions.
"""

from __future__ import annotations

import base64
import logging
import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The handler module's logger, kept so the log lines read the same as before.
logger = logging.getLogger("robothor.engine.tools.handlers.gws")


# ── Turning a Gmail API message into something an agent can read ──────
#
# The Gmail API returns a MIME tree with every body base64url-encoded, which is
# the correct wire format and an unreadable tool result. Everything below
# converts one of those into flat fields, once, in the handler — the place that
# knows the cap it has to fit inside.


class _HtmlToText(HTMLParser):
    """The smallest HTML-to-text converter that does not lie.

    Stdlib only, on purpose: an HTML-only email is a routine thing (an invoice,
    a calendar invite, anything sent by a marketing system) and pulling a
    parser dependency into the engine for it would be a supply-chain decision
    made by a mail format. It drops ``script`` and ``style`` contents — those
    are not the email, and handing a model the contents of a ``<script>`` tag
    out of untrusted mail is an injection surface — turns block-level tags into
    newlines, and unescapes entities (``convert_charrefs`` does that for us).

    **Suppression is a STACK of open element names, not a depth counter.**

    The counter only ever came down on a matching ``handle_endtag``, so any
    suppressing element that was never explicitly closed pinned it above zero
    for the rest of the document and the body came back empty. Three ways that
    happens in ordinary mail, none of them exotic:

    * ``</head>`` and ``</title>`` are **omissible in HTML5** and real mail
      generators omit them. Valid HTML5 with no ``</head>`` returned "".
    * a generator self-closes ``<style/>``, ``<script/>`` or ``<head/>``
      (XHTML habits survive in mail templates).
    * the mail is simply malformed, which is the normal case here.

    The first round of this fix exempted the void elements, which was one
    trigger of the same bug rather than the bug. A stack fixes the class: a
    region is closed by its own end tag, by an **implicit close** — a start tag
    that cannot legally appear inside it, which is how HTML5 says ``<body>``
    ends an unclosed ``<head>`` — or by the end of the document, at which point
    whatever is still open has no content left to suppress anyway.

    ``body_text: ""`` with ``body_chars: 0`` and ``body_truncated: false`` is
    indistinguishable from a genuinely blank message, so the agent reports real
    mail as empty. That is the one failure this converter exists to prevent.
    """

    #: Elements with no end tag (HTML5's void elements). ``HTMLParser`` reports
    #: them through ``handle_starttag`` alone unless they are written
    #: self-closing, so none of them may ever open a region.
    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    #: Elements whose CONTENT is not the email.
    _DROP = frozenset({"script", "style", "head", "title"})

    _BREAK = frozenset(
        {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    )

    #: Flow content: a start tag that cannot appear inside ``head`` or ``title``,
    #: so seeing one means the unclosed element ended. ``body`` is the canonical
    #: case — HTML5 says an omitted ``</head>`` is implied by it — but mail
    #: generators also drop straight into a ``<table>`` or a ``<div>``.
    _FLOW = frozenset(
        {
            "body",
            "div",
            "p",
            "table",
            "tbody",
            "thead",
            "tr",
            "td",
            "th",
            "span",
            "a",
            "ul",
            "ol",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "center",
            "font",
            "article",
            "section",
            "main",
            "header",
            "footer",
        }
    )

    #: What each suppressing element may legally contain. Anything outside it
    #: implicitly closes the element. ``script`` and ``style`` contain only
    #: character data, so nothing closes them but their own end tag — a start
    #: tag inside one is script text, not markup.
    _MAY_CONTAIN: dict[str, frozenset[str]] = {
        "head": frozenset({"title", "meta", "link", "style", "script", "base", "noscript"}),
        "title": frozenset(),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._open: list[str] = []

    @property
    def _suppressed(self) -> bool:
        return bool(self._open)

    def _implicitly_close(self, tag: str) -> None:
        """Pop any open region that ``tag`` cannot legally appear inside.

        ``<body>`` after an unclosed ``<head>``, or any flow content after an
        unclosed ``<title>``. Character-data elements (``script``, ``style``)
        are never closed this way: their contents are text, not tags.
        """
        while self._open:
            top = self._open[-1]
            allowed = self._MAY_CONTAIN.get(top)
            if allowed is None:
                return  # script/style — only its own end tag closes it
            if tag in allowed:
                return
            if tag in self._FLOW or tag == "body":
                self._open.pop()
                continue
            return

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self._implicitly_close(tag)
        if tag in self._VOID:
            # Never opens a region. `br` is also in _BREAK and still breaks.
            if tag in self._BREAK and not self._suppressed:
                self._chunks.append("\n")
            if tag == "img" and not self._suppressed:
                self._emit_alt(attrs)
            return
        if tag in self._DROP:
            self._open.append(tag)
            return
        if tag in self._BREAK and not self._suppressed:
            self._chunks.append("\n")

    def _emit_alt(self, attrs: Any) -> None:
        """An image's ``alt`` text, when it has any.

        A body that is one image returned an empty ``body_text`` — true, and
        indistinguishable from the empty-body bug this parser was rewritten to
        fix. ``alt`` is the sender's own description of the image and is the
        only text such a mail has.

        An EMPTY ``alt`` is skipped, not emitted: ``alt=""`` is the HTML
        convention for "this image carries no meaning", and a marketing mail
        with a spacer gif per row would otherwise fill the body with noise.
        """
        for name, value in attrs or ():
            if name == "alt" and value and str(value).strip():
                self._chunks.append(f"[image: {str(value).strip()}]")
                return

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        """``<style/>``, ``<head/>``, ``<meta … />``.

        A self-closing tag opens and closes in one token, so it must never push
        a region. Delegating to ``handle_starttag`` — which is what the previous
        version did, under a docstring claiming this exact property — pushed
        ``style``/``script``/``head`` and lost the rest of the document.
        """
        self._implicitly_close(tag)
        if tag in self._BREAK and not self._suppressed:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag in self._DROP:
            # Pop to and including this tag if it is open at all. An end tag
            # for something never opened (`</style>` alone) is ignored.
            if tag in self._open:
                while self._open and self._open.pop() != tag:
                    pass
            return
        if tag == "head":
            self._open.clear()
            return
        if tag in self._BREAK and not self._suppressed:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


#: Elements whose contents ``HTMLParser`` hands over as one blob of character
#: data rather than parsing (``CDATA_CONTENT_ELEMENTS``, plus ``title``, which
#: behaves the same way in practice). Inside one of these the parser stops
#: seeing tags entirely, so an element that is never closed swallows the whole
#: rest of the document — no ``handle_starttag`` for ``<body>`` ever arrives,
#: and no amount of bookkeeping in the handler can recover.
#:
#: They are therefore removed BEFORE parsing, closed or not.
_NON_CONTENT = "script|style|title|xmp|iframe|noembed|noframes"

#: A properly closed one.
_CLOSED_REGION_RE = re.compile(rf"<({_NON_CONTENT})\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

#: A self-closed one: ``<style/>``. It has no contents to remove, only a tag.
_SELF_CLOSED_RE = re.compile(rf"<(?:{_NON_CONTENT}|head)\b[^>]*/\s*>", re.IGNORECASE)

#: An opener with no matching close. It runs to whatever ends the head — or to
#: the end of the document, which is the honest reading of "the author never
#: closed it": everything after is inside the element as far as any parser is
#: concerned, so the only question is where a HUMAN would say it stopped.
_UNCLOSED_REGION_RE = re.compile(
    rf"<(?:{_NON_CONTENT})\b[^>]*>.*?(?=</head\s*>|<body\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_non_content_regions(html: str) -> str:
    """Remove script/style/title regions, whether or not they are closed.

    This is what makes an unclosed ``<style>`` survivable. ``HTMLParser`` puts
    those elements into character-data mode, so ``<html><head><style>p{x}</head>
    <body><p>REAL PROSE</p>`` delivers the entire rest of the document as one
    `handle_data` call with `style` still open, and the body comes back empty.
    Every one of the seven triggers in the re-review is this shape or the
    omitted-``</head>`` shape; the handler's stack fixes the second, and only a
    pre-pass can fix the first.
    """
    html = _CLOSED_REGION_RE.sub(" ", html)
    html = _SELF_CLOSED_RE.sub(" ", html)
    return _UNCLOSED_REGION_RE.sub(" ", html)


def _html_to_text(html: str) -> str:
    """Readable text from an HTML email body. Never raises on bad markup."""
    parser = _HtmlToText()
    try:
        parser.feed(_strip_non_content_regions(html))
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed mail is the normal case
        logger.debug("gws: HTML body could not be parsed (%s); falling back", exc)
        return re.sub(r"<[^>]+>", " ", html).strip()
    return parser.text()


def _decode_b64(data: str) -> str:
    """Decode one base64url MIME part. Never raises: a body that will not
    decode is still a message with headers worth returning."""
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 - a mangled part is not a crash
        logger.debug("gws: body part would not base64-decode (%s)", exc)
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _walk_parts(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every MIME part of a message, depth first, the payload itself included."""
    yield payload
    for part in payload.get("parts") or ():
        if isinstance(part, dict):
            yield from _walk_parts(part)


def _extract_body(payload: dict[str, Any]) -> str:
    """The message's text: ``text/plain`` if it has one, else its HTML as text.

    Preferring plain text is not an aesthetic choice — the HTML alternative of
    the same message is the same words wrapped in three times the characters,
    and the budget is 2,500 of them.
    """
    plain: list[str] = []
    html: list[str] = []
    for part in _walk_parts(payload):
        if part.get("filename"):
            continue  # an attachment, not the message
        mime = str(part.get("mimeType", ""))
        data = str((part.get("body") or {}).get("data", ""))
        if not data:
            continue
        if mime.startswith("text/plain"):
            plain.append(_decode_b64(data))
        elif mime.startswith("text/html"):
            html.append(_decode_b64(data))
    if plain:
        return "\n".join(t for t in plain if t).strip()
    if html:
        return _html_to_text("\n".join(html))
    return ""


def _extract_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Attachments by name, type and size. Never their bytes: one PDF would be
    the whole result, and the agent has `analyze_pdf` for the contents."""
    found: list[dict[str, Any]] = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "")
        if not filename:
            continue
        body = part.get("body") or {}
        found.append(
            {
                "filename": filename,
                "mime_type": str(part.get("mimeType", "")),
                "size_bytes": int(body.get("size") or 0),
            }
        )
    return found


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Headers keyed lowercase. A message with no headers at all is a real
    shape the API returns, and it must not take the tool down."""
    out: dict[str, str] = {}
    for header in payload.get("headers") or ():
        if isinstance(header, dict) and "name" in header:
            out[str(header["name"]).lower()] = str(header.get("value", ""))
    return out


def _shape_envelope(
    message: dict[str, Any], *, max_header_chars: int | None = None
) -> dict[str, Any]:
    """The described-but-unread form: who, when, about what, and its labels.

    ``max_header_chars`` bounds each header, for the search path: a ``To:``
    addressed to a distribution list is unbounded in the API and one of them
    used to consume the whole result budget. ``gws_gmail_get`` passes nothing
    and keeps the headers whole — asking for one message is asking for all of
    it.
    """

    def cut(value: str) -> str:
        if max_header_chars is None or len(value) <= max_header_chars:
            return value
        return value[:max_header_chars] + "…"

    payload = message.get("payload") or {}
    headers = _header_map(payload)
    return {
        "id": str(message.get("id", "")),
        "thread_id": str(message.get("threadId", "")),
        "date": headers.get("date", ""),
        "from": cut(headers.get("from", "")),
        "to": cut(headers.get("to", "")),
        "subject": cut(headers.get("subject", "")),
        # NOT unescaped: `html.unescape` turns `&lt;script&gt;` back into real
        # `<script>` markup, reconstructing from untrusted mail exactly what the
        # body path deliberately strips. Gmail's snippet is already text with
        # its entities escaped; leaving them escaped is both safe and readable.
        "snippet": cut(str(message.get("snippet", ""))),
        "labels": [str(label) for label in (message.get("labelIds") or [])],
    }


def _shape_message(message: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """One message, whole: envelope, cc, message-id, decoded body, attachments."""
    payload = message.get("payload") or {}
    headers = _header_map(payload)
    shaped = _shape_envelope(message)
    shaped["cc"] = headers.get("cc", "")
    shaped["message_id"] = headers.get("message-id", "")

    body = _extract_body(payload)
    shaped["body_chars"] = len(body)
    if max_chars >= 0 and len(body) > max_chars:
        shaped["body_text"] = body[:max_chars]
        shaped["body_truncated"] = True
    else:
        shaped["body_text"] = body
        shaped["body_truncated"] = False

    attachments = _extract_attachments(payload)
    if attachments:
        shaped["attachments"] = attachments
    return shaped
