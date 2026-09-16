"""`exec` output that was cut has to say so.

The handler sliced stdout to 4,000 characters and stderr to 2,000 with a bare
`[:4000]` — no marker, no count, nothing in the result. The model could not
distinguish "the command printed 40 lines" from "the command printed 4,000
lines and you are seeing the first eight percent".

MEASURED 2026-09-16: the four benchmark tasks that scored worst were all bulk
extraction over 21 to 130 documents, with 57 to 70 `exec` calls each. Every
listing, every dump and every diagnostic on those runs silently lost its tail,
and the agent reasoned from the visible part as though it were the whole.

Amputation the model cannot see. The remedy is not a bigger slice — a bigger
slice has the same defect one order of magnitude later — it is that the cut is
named, counted, and paired with what to do instead.
"""

from __future__ import annotations

from robothor.engine.tools.handlers.filesystem import (
    STDERR_LIMIT,
    STDOUT_LIMIT,
    truncate_stream,
)


class TestShortOutputIsUntouched:
    def test_it_passes_through_verbatim(self):
        assert truncate_stream("hello\n", STDOUT_LIMIT) == "hello\n"

    def test_empty_stays_empty(self):
        assert truncate_stream("", STDOUT_LIMIT) == ""

    def test_exactly_at_the_limit_is_not_marked(self):
        """A marker on output that lost nothing is a lie in the other
        direction, and it is the one that erodes trust in the marker."""
        text = "x" * STDOUT_LIMIT
        assert truncate_stream(text, STDOUT_LIMIT) == text


class TestTruncatedOutputSaysSo:
    def test_the_marker_is_present(self):
        out = truncate_stream("x" * 10_000, STDOUT_LIMIT)
        assert "[truncated:" in out

    def test_it_counts_both_sides(self):
        """ "Some output was cut" is unactionable. How much, out of how much,
        is the number that tells the model whether to narrow or to page."""
        out = truncate_stream("x" * 10_000, STDOUT_LIMIT)
        assert f"{STDOUT_LIMIT} of 10000 chars shown" in out

    def test_it_names_the_remedy(self):
        """The failure was the model reasoning from a decapitated listing as
        though it were complete. Telling it only that it is incomplete leaves
        it in the same place."""
        out = truncate_stream("x" * 10_000, STDOUT_LIMIT)
        assert "narrower command" in out
        assert "read_file" in out

    def test_the_visible_part_is_still_the_head(self):
        out = truncate_stream("A" + "x" * 10_000, STDOUT_LIMIT)
        assert out.startswith("A" + "x" * 100)

    def test_the_result_never_exceeds_the_limit_by_much(self):
        """The cap exists to bound context. A marker that made the result
        bigger than the thing it was bounding would be its own defect."""
        out = truncate_stream("x" * 10_000_000, STDOUT_LIMIT)
        assert len(out) < STDOUT_LIMIT + 300


class TestTheLimitsAreNamedConstants:
    def test_both_streams_have_one(self):
        """Two bare literals inside a subprocess result is how this went
        unnoticed: nothing named them, so nothing could test them."""
        assert STDOUT_LIMIT > 0
        assert STDERR_LIMIT > 0

    def test_the_handler_uses_them(self):
        import inspect

        from robothor.engine.tools.handlers import filesystem

        source = inspect.getsource(filesystem)
        assert "proc.stdout[:4000]" not in source
        assert "truncate_stream(proc.stdout" in source
        assert "truncate_stream(proc.stderr" in source
