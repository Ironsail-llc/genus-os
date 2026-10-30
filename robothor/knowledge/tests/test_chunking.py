"""chunk_text: deterministic, overlapping, word-bounded windows."""

from __future__ import annotations

import pytest

from robothor.knowledge.chunking import chunk_text


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def test_empty_and_blank_text_have_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\t ") == []


def test_short_text_is_one_chunk_kept_verbatim():
    text = "Refunds are issued within 14 days.\n\nContact support first."
    assert chunk_text(text) == [text]


def test_default_window_is_about_400_tokens_with_40_overlap():
    chunks = chunk_text(_words(1000))
    sizes = [len(c.split()) for c in chunks]
    assert max(sizes) == 400
    first, second = chunks[0].split(), chunks[1].split()
    assert first[-40:] == second[:40]


def test_windows_overlap_and_cover_every_word_in_order():
    text = _words(25)
    chunks = chunk_text(text, max_tokens=10, overlap=3)
    words = [c.split() for c in chunks]
    assert all(len(w) <= 10 for w in words)
    for prev, nxt in zip(words, words[1:], strict=False):
        assert prev[-3:] == nxt[:3]
    # Stitching the windows back together (dropping each overlap) is the text.
    stitched = words[0] + [w for chunk in words[1:] for w in chunk[3:]]
    assert stitched == text.split()


def test_chunking_is_deterministic():
    text = _words(900)
    assert chunk_text(text, max_tokens=50, overlap=5) == chunk_text(text, max_tokens=50, overlap=5)


def test_line_breaks_inside_a_chunk_are_preserved():
    text = "line one\nline two\n\nline three"
    assert chunk_text(text, max_tokens=100, overlap=0) == [text]


@pytest.mark.parametrize(("max_tokens", "overlap"), [(0, 0), (10, 10), (10, -1)])
def test_invalid_window_is_refused(max_tokens, overlap):
    with pytest.raises(ValueError):
        chunk_text("some text", max_tokens=max_tokens, overlap=overlap)
