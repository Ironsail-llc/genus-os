"""Markdown → Telegram, rendered for a phone.

The model writes GitHub Markdown; Telegram's HTML parse mode understands a
dozen tags and nothing else. This module is the one place that bridges the
two, for every Telegram send and every delivery-count prediction:

- ``render_telegram_html`` parses with markdown-it (CommonMark + GFM tables
  and strikethrough) and walks the tree: headings become a bold line, ``---``
  a thin rule, lists ``•``/``N.`` with nesting, tables one card per row (a
  pipe grid is unreadable at phone width), ``>`` a ``<blockquote>``.
- ``telegram_chunks`` renders FIRST and splits the HTML second, closing every
  open tag at a cut and reopening it in the next chunk, measuring the length
  Telegram measures. Splitting Markdown and converting each piece (the old
  way) cut bold spans in half; Telegram refused the HTML and the raw Markdown
  went out instead.
- Each chunk carries a ``plain`` twin — tags stripped, links as
  ``label (url)`` — for the rare chunk Telegram still refuses, so the fallback
  is never raw Markdown.
- ``needs_rich_message`` says when a body is better sent through Bot API
  10.1 ``sendRichMessage``, which renders a real table natively.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from robothor.engine.chunking import strip_canvas_markers

#: Per-message budget, under Telegram's 4096 so a reopened tag or a client's
#: own counting never tips a chunk over (OpenClaw uses the same margin).
TELEGRAM_CHUNK_LIMIT = 4000

#: ``sendRichMessage`` accepts up to this many characters of Markdown.
RICH_MESSAGE_MAX_CHARS = 32768

_SAFE_LINK = re.compile(r"^(https?:|tg:|mailto:|tel:)", re.IGNORECASE)
_SPOILER = re.compile(r"\|\|(.+?)\|\|")
_TASK = re.compile(r"^\[([ xX])\]\s+")
_INDEX = re.compile(r"^#?\d{0,3}[.)]?$")
_RULE = "───────"

_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


# ── Rendering ────────────────────────────────────────────────────────────


def _inline(node: SyntaxTreeNode, *, bold: bool = True) -> str:
    """Render an inline node's children. ``bold=False`` drops ``<b>`` so a
    heading or card title, already bold, doesn't nest it."""
    out: list[str] = []
    for child in node.children:
        kind = child.type
        if kind == "text":
            out.append(_SPOILER.sub(r"<tg-spoiler>\1</tg-spoiler>", _esc(child.content)))
        elif kind in ("softbreak", "hardbreak"):
            out.append("\n")
        elif kind == "code_inline":
            out.append(f"<code>{_esc(child.content)}</code>")
        elif kind == "strong":
            inner = _inline(child, bold=bold)
            out.append(f"<b>{inner}</b>" if bold else inner)
        elif kind == "em":
            out.append(f"<i>{_inline(child, bold=bold)}</i>")
        elif kind == "s":
            out.append(f"<s>{_inline(child, bold=bold)}</s>")
        elif kind == "link":
            href = str(child.attrs.get("href", ""))
            label = _inline(child, bold=bold)
            if _SAFE_LINK.match(href):
                out.append(f'<a href="{html.escape(href, quote=True)}">{label}</a>')
            else:
                out.append(label)
        elif kind == "image":
            src = str(child.attrs.get("src", ""))
            alt = _esc(child.content) or "image"
            out.append(
                f'<a href="{html.escape(src, quote=True)}">{alt}</a>'
                if _SAFE_LINK.match(src)
                else alt
            )
        elif child.children:
            out.append(_inline(child, bold=bold))
        else:
            out.append(_esc(child.content))
    return "".join(out)


def _inline_of(node: SyntaxTreeNode, *, bold: bool = True) -> str:
    """Inline text of a block that wraps exactly one inline node (paragraph,
    heading, table cell)."""
    return "".join(_inline(c, bold=bold) for c in node.children if c.type == "inline")


def _cell_text(node: SyntaxTreeNode, *, bold: bool = True) -> str:
    return _inline_of(node, bold=bold).strip()


def _table(node: SyntaxTreeNode) -> str:
    """One card per row: a bold title, then ``Header: value`` lines.

    An index column (``1``, ``#2``) folds into the title with the next cell, so ``| 1 | Alpha | $8k |`` reads ``1. Alpha`` / ``Price: $8k``.
    """
    headers: list[str] = []
    rows: list[list[SyntaxTreeNode]] = []
    for section in node.children:
        for tr in section.children:
            cells = list(tr.children)
            if section.type == "thead":
                headers = [_cell_text(c, bold=False) for c in cells]
            else:
                rows.append(cells)
    cards: list[str] = []
    for cells in rows:
        titles = [_cell_text(c, bold=False) for c in cells]
        values = [_cell_text(c, bold=False) for c in cells]
        start = 1
        title = titles[0] if titles else ""
        if len(titles) > 1 and _INDEX.match(title) and titles[1]:
            title = f"{title}. {titles[1]}" if title else titles[1]
            start = 2
        lines = [f"<b>{title}</b>"] if title else []
        for i in range(start, len(values)):
            if not values[i]:
                continue
            label = headers[i] if i < len(headers) and headers[i] else ""
            lines.append(f"{label}: {values[i]}" if label else values[i])
        cards.append("\n".join(lines))
    return "\n\n".join(c for c in cards if c)


def _list(node: SyntaxTreeNode, depth: int) -> str:
    ordered = node.type == "ordered_list"
    number = int(node.attrs.get("start", 1) or 1) if ordered else 0
    indent = "  " * depth
    items: list[str] = []
    for item in node.children:
        parts = [_block(child, depth + 1) for child in item.children]
        body = "\n".join(p for p in parts if p)
        task = _TASK.match(body)
        if task:
            marker = "☑ " if task.group(1).lower() == "x" else "☐ "
            body = body[task.end() :]
        elif ordered:
            marker = f"{number}. "
        else:
            marker = "• " if depth == 0 else "◦ "
        items.append(f"{indent}{marker}{body}")
        number += 1
    return "\n".join(items)


def _blocks(nodes: list[SyntaxTreeNode], depth: int) -> str:
    return "\n\n".join(r for r in (_block(n, depth) for n in nodes) if r)


def _block(node: SyntaxTreeNode, depth: int = 0) -> str:
    kind = node.type
    if kind == "paragraph":
        return _inline_of(node)
    if kind == "heading":
        text = _inline_of(node, bold=False).strip()
        return f"<b>{text}</b>" if text else ""
    if kind == "hr":
        return _RULE
    if kind in ("bullet_list", "ordered_list"):
        return _list(node, depth)
    if kind == "blockquote":
        return f"<blockquote>{_blocks(node.children, 0)}</blockquote>"
    if kind in ("fence", "code_block"):
        code = _esc(node.content.rstrip("\n"))
        lang = (node.info or "").strip().split(" ")[0] if kind == "fence" else ""
        if lang:
            return f'<pre><code class="language-{_esc(lang)}">{code}</code></pre>'
        return f"<pre>{code}</pre>"
    if kind == "table":
        return _table(node)
    if kind in ("html_block", "html_inline"):
        return _esc(node.content.strip("\n"))
    if kind == "inline":
        return _inline(node)
    return _blocks(node.children, depth)


def render_telegram_html(text: str) -> str:
    """Markdown → Telegram HTML. Canvas markers are dropped first."""
    text = strip_canvas_markers(text or "")
    if not text.strip():
        return ""
    tree = SyntaxTreeNode(_md.parse(text))
    return _blocks(tree.children, 0).strip("\n")


# ── Plain-text twin ──────────────────────────────────────────────────────


class _Plain(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._links: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._links.append((dict(attrs).get("href") or "", len(self.out)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._links:
            href, start = self._links.pop()
            label = "".join(self.out[start:])
            if href and href != label:
                self.out.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def html_to_plain(rendered: str) -> str:
    """Telegram HTML → what it says, for a chunk Telegram refused as HTML."""
    parser = _Plain()
    parser.feed(rendered)
    parser.close()
    return "".join(parser.out)


# ── Render-aware splitting ───────────────────────────────────────────────

_UNIT = re.compile(r"<[^>]+>|&[a-zA-Z#0-9]+;|[\s\S]", re.DOTALL)


def _units(rendered: str) -> list[str]:
    """Indivisible pieces: a whole tag, a whole entity, or one character."""
    return _UNIT.findall(rendered)


def _tag_name(tag: str) -> str:
    return tag.strip("</>").split()[0].lower()


def _size(piece: str) -> int:
    """Telegram counts UTF-16 code units; an emoji is two."""
    return len(piece.encode("utf-16-le")) // 2


def split_telegram_html(rendered: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split Telegram HTML into balanced chunks of at most ``limit``.

    Prefers a paragraph break, then a line break, then a space, in the back
    two-thirds of the window; otherwise cuts hard between units. Tags open at
    a cut are closed there and reopened at the start of the next chunk.
    """
    if _size(rendered) <= limit:
        return [rendered] if rendered.strip() else []
    units = _units(rendered)
    # stacks[i] = opening tags in force before unit i.
    stacks: list[tuple[str, ...]] = []
    stack: list[str] = []
    for unit in units:
        stacks.append(tuple(stack))
        if unit.startswith("</"):
            if stack:
                stack.pop()
        elif unit.startswith("<"):
            stack.append(unit)
    stacks.append(tuple(stack))

    def closers(i: int) -> str:
        return "".join(f"</{_tag_name(t)}>" for t in reversed(stacks[i]))

    chunks: list[str] = []
    start = 0
    count = len(units)
    while start < count:
        while start < count and units[start].isspace():
            start += 1
        if start >= count:
            break
        prefix = "".join(stacks[start])
        size = _size(prefix)
        para = line = space = -1
        i = start
        while i < count:
            grown = size + _size(units[i])
            if grown + _size(closers(i + 1)) > limit and i > start:
                break
            size = grown
            if units[i] == "\n":
                line = i + 1
                if i > start and units[i - 1] == "\n":
                    para = i + 1
            elif units[i].isspace():
                space = i + 1
            i += 1
        if i >= count:
            cut = count
        else:
            floor = start + (i - start) // 3
            cut = next((c for c in (para, line, space) if c > floor), i)
        end = cut
        while end > start and units[end - 1].isspace():
            end -= 1
        body = "".join(units[start:end])
        piece = prefix + body + closers(end)
        if body.strip():
            chunks.append(piece)
        start = cut
    return chunks


@dataclass(frozen=True)
class TelegramChunk:
    """One Telegram message: the HTML to send, and its plain-text twin."""

    html: str
    plain: str


def telegram_chunks(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[TelegramChunk]:
    """Render ``text`` and split it into the messages Telegram will receive."""
    return [
        TelegramChunk(html=piece, plain=html_to_plain(piece))
        for piece in split_telegram_html(render_telegram_html(text), limit)
    ]


# ── Rich messages (Bot API 10.1) ─────────────────────────────────────────


def rich_messages_enabled() -> bool:
    from robothor.settings import get_settings

    return bool(get_settings().channels.telegram_rich_messages)


def needs_rich_message(text: str) -> bool:
    """True when ``text`` holds a Markdown table Telegram can draw natively.

    Ordinary replies stay on HTML (consistent look, no new failure mode);
    only a table is worth the rich endpoint.
    """
    if not text or not rich_messages_enabled():
        return False
    body = strip_canvas_markers(text)
    if "|" not in body or len(body) > RICH_MESSAGE_MAX_CHARS:
        return False
    return any(token.type == "table_open" for token in _md.parse(body))


def rich_markdown(text: str) -> str:
    """The Markdown body for ``sendRichMessage``.

    The rich renderer treats a lone newline as a soft break (a space), but the
    model means a new line, so end each such line with a hard break — except
    inside code fences and tables, where the newline is already structural.
    """
    lines = strip_canvas_markers(text).split("\n")
    out: list[str] = []
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        nxt = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if (
            not in_fence
            and stripped
            and nxt
            and not nxt.startswith(("```", "~~~"))
            and "|" not in stripped
            and not line.endswith(("  ", "\\"))
        ):
            line += "  "
        out.append(line)
    return "\n".join(out)


def rich_payload(text: str) -> dict[str, Any]:
    """Keyword arguments for ``InputRichMessage``."""
    return {"markdown": rich_markdown(text)}


def planned_message_count(text: str) -> int:
    """How many Telegram messages ``TelegramBot.send_message`` plans for
    ``text``: one rich message for a table body, else one per chunk.

    The delivery layer compares this against what the sender acknowledged; a
    rich send that Telegram refuses falls back to chunks, and the sender then
    reports its real plan on ``SentMessages.expected``.
    """
    if needs_rich_message(text):
        return 1
    return len(telegram_chunks(text))


class SentMessages(list[Any]):
    """The ``Message`` objects Telegram acknowledged, plus how many the sender
    meant to deliver — the two numbers a delivery receipt compares."""

    def __init__(self, messages: list[Any] | None = None, *, expected: int = 0) -> None:
        super().__init__(messages or [])
        self.expected = expected
