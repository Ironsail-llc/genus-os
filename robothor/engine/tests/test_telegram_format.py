"""Telegram rendering: what the operator reads on a phone.

Live, 2026-10-08: the operator called main's Telegram replies "almost always
impossible to read". The model writes desktop GitHub Markdown — pipe tables,
``---`` rules, ``#`` headings, nested bullets — and the old converter only
knew bold, italic, code and links, so the rest arrived as pipe-and-dash soup.
Worse, the reply was split as Markdown and each piece converted afterwards: a
bold span cut at the boundary became unbalanced HTML, Telegram refused it, and
the fallback resent the RAW Markdown.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

from robothor.engine.telegram_format import (
    TELEGRAM_CHUNK_LIMIT,
    html_to_plain,
    needs_rich_message,
    render_telegram_html,
    telegram_chunks,
)

if TYPE_CHECKING:
    import pytest

# Shape of the real reply (anonymized): a five-column table, rules, emoji
# headings, bold everywhere, a closing question.
SHORTLIST = """Here it is — priced, rated, and ranked. **The score is mine**, not a review score.

---

**🟢 IN BUDGET — the buys**

| # | Machine | Price | Score | Small details |
|---|---|---|---|---|
| **1** | **Alpha "Pilot"** (Shenzhen) | **$7.9–11.6k** | **8.5** | 6-axis **double-arm**; MOQ 1 |
| **2** | **Beta Unit** | **$16.8–19.8k** | **8.0** | 10-yr mfr, **4.5/5** ⚠️ single-arm |

**⛔ SKIP**
- **Gamma** $11–12k — **MOQ 5**, can't buy one.
- **Delta** $13.5k — a *food-machinery* company.

---

**Want me to take #1 and #2 to firm quotes now? y/n**
"""


class _TagBalance(HTMLParser):
    """Records whether every opened tag is closed, in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ok = True

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.ok = False


def _balanced(html: str) -> bool:
    parser = _TagBalance()
    parser.feed(html)
    parser.close()
    return parser.ok and not parser.stack


class TestRender:
    def test_a_table_becomes_row_cards_without_pipes(self) -> None:
        out = render_telegram_html(SHORTLIST)
        assert "|" not in out
        assert "---" not in out
        # A short index column folds into the card title; the rest are labeled.
        assert '<b>1. Alpha "Pilot" (Shenzhen)</b>' in out
        assert "Price: $7.9–11.6k" in out and "Score: 8.5" in out
        assert "$7.9–11.6k" in out
        assert _balanced(out)

    def test_horizontal_rule_becomes_a_thin_line(self) -> None:
        assert "───" in render_telegram_html("one\n\n---\n\ntwo")

    def test_heading_becomes_a_bold_line(self) -> None:
        out = render_telegram_html("## Next steps\n\nCall them.")
        assert "<b>Next steps</b>" in out
        assert "#" not in out

    def test_heading_with_inner_bold_has_no_nested_bold(self) -> None:
        out = render_telegram_html("### **Bottom line**")
        assert out.count("<b>") == 1

    def test_bullets_and_numbers(self) -> None:
        out = render_telegram_html("- one\n- two\n  - nested\n\n1. first\n2. second")
        assert "• one" in out and "• two" in out
        assert "  • nested" in out or "  ◦ nested" in out
        assert "1. first" in out and "2. second" in out
        assert "- one" not in out

    def test_task_list(self) -> None:
        out = render_telegram_html("- [ ] todo\n- [x] done")
        assert "☐ todo" in out and "☑ done" in out

    def test_blockquote_strike_spoiler(self) -> None:
        out = render_telegram_html("> quoted\n\n~~gone~~ and ||secret||")
        assert "<blockquote>quoted</blockquote>" in out
        assert "<s>gone</s>" in out
        assert "<tg-spoiler>secret</tg-spoiler>" in out

    def test_code_and_fences(self) -> None:
        out = render_telegram_html("Run `ls -la`\n\n```python\nx = 1 < 2\n```")
        assert "<code>ls -la</code>" in out
        assert '<pre><code class="language-python">x = 1 &lt; 2</code></pre>' in out

    def test_links_only_for_safe_schemes(self) -> None:
        out = render_telegram_html("[site](https://example.com) and [bad](javascript:alert(1))")
        assert '<a href="https://example.com">site</a>' in out
        assert 'href="javascript' not in out
        assert "bad" in out

    def test_raw_html_from_the_model_is_escaped(self) -> None:
        out = render_telegram_html("a <script>x</script> & b")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out and "&amp;" in out

    def test_bold_and_italic(self) -> None:
        out = render_telegram_html("**bold** and *italic* and __also bold__")
        assert "<b>bold</b>" in out and "<i>italic</i>" in out

    def test_snake_case_is_not_italicised(self) -> None:
        assert "<i>" not in render_telegram_html("set my_var_name now")

    def test_plain_prose_is_unchanged(self) -> None:
        assert render_telegram_html("Hello there.\nSecond line.") == "Hello there.\nSecond line."

    def test_canvas_markers_never_render(self) -> None:
        out = render_telegram_html('Done.\n[RENDER:metric-grid:{"items":[]}]')
        assert "RENDER" not in out

    def test_shortlist_reads_cleanly(self) -> None:
        out = render_telegram_html(SHORTLIST)
        for junk in ("**", "| ", "|---", "\n- ", "###"):
            assert junk not in out


class TestChunks:
    def test_short_message_is_one_chunk(self) -> None:
        chunks = telegram_chunks("hello **you**")
        assert len(chunks) == 1
        assert chunks[0].html == "hello <b>you</b>"
        assert chunks[0].plain == "hello you"

    def test_empty_and_marker_only_bodies_are_no_chunks(self) -> None:
        assert telegram_chunks("") == []
        assert telegram_chunks('[DASHBOARD:{"a":1}]') == []

    def test_every_chunk_is_balanced_and_within_limit(self) -> None:
        # Bold, code and a fence all straddle likely cut points.
        para = "**" + ("word & more <stuff> " * 60) + "** then `code` here.\n\n"
        body = para * 12 + "```\n" + ("line & <x>\n" * 400) + "```\n"
        chunks = telegram_chunks(body)
        assert len(chunks) > 2
        for chunk in chunks:
            assert len(chunk.html) <= TELEGRAM_CHUNK_LIMIT
            assert _balanced(chunk.html), chunk.html[:200]
            assert len(chunk.plain) <= TELEGRAM_CHUNK_LIMIT

    def test_one_huge_bold_span_is_reopened_in_the_next_chunk(self) -> None:
        chunks = telegram_chunks("**" + ("abc " * 3000).strip() + "**")
        assert len(chunks) >= 3
        assert all(c.html.startswith("<b>") and c.html.endswith("</b>") for c in chunks)

    def test_ampersand_heavy_text_measures_after_escaping(self) -> None:
        chunks = telegram_chunks("& " * 3000)
        assert all(len(c.html) <= TELEGRAM_CHUNK_LIMIT for c in chunks)

    def test_no_word_is_cut_when_whitespace_exists(self) -> None:
        words = [f"w{i}" for i in range(3000)]
        chunks = telegram_chunks(" ".join(words))
        rejoined = " ".join(c.plain for c in chunks).split()
        assert rejoined == words

    def test_entity_is_never_split(self) -> None:
        for chunk in telegram_chunks("x" * 3999 + "&&&&"):
            assert not chunk.html.endswith("&amp") and not chunk.html.startswith("amp;")


class TestPlain:
    def test_plain_has_no_markup(self) -> None:
        plain = html_to_plain(render_telegram_html(SHORTLIST))
        for junk in ("*", "<", "|", "&amp;", "───\n|"):
            assert junk not in plain
        assert "$7.9–11.6k" in plain

    def test_links_keep_their_url(self) -> None:
        plain = html_to_plain(render_telegram_html("[site](https://example.com)"))
        assert plain == "site (https://example.com)"


class TestRichMessages:
    def test_a_table_wants_a_rich_message(self) -> None:
        assert needs_rich_message(SHORTLIST)

    def test_prose_does_not(self) -> None:
        assert not needs_rich_message("Just **words** and\n- a list")

    def test_pipes_in_prose_are_not_a_table(self) -> None:
        assert not needs_rich_message("a | b is not a table")

    def test_oversized_table_message_does_not(self) -> None:
        assert not needs_rich_message(SHORTLIST + "x" * 40000)

    def test_flag_turns_it_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_TELEGRAM_RICH_MESSAGES", "0")
        reset_settings()
        try:
            assert not needs_rich_message(SHORTLIST)
        finally:
            monkeypatch.delenv("ROBOTHOR_TELEGRAM_RICH_MESSAGES")
            reset_settings()
