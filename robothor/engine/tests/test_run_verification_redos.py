"""The claim-extraction regexes must not be super-linear in their input.

CodeQL flagged two patterns in `run_verification.py` as polynomial
backtracking. One of them measurably is: `_IMPERATIVE_STEP_RE` wrote
`^\\s*(?:...)?\\s*`, two whitespace runs either side of an optional group, so a
line of leading whitespace can be split between them N+1 ways.

The tests below pin the BEHAVIOUR each pattern was written for, so a rewrite
for complexity cannot quietly change what counts as a claim, plus a timing
bound for the pattern whose cost is actually observable.
"""

from __future__ import annotations

from robothor.engine.run_verification import _subject_is_someone_else


class TestTheAdverbIsStillDropped:
    """`_subject_is_someone_else` looks past a trailing adverb to find its
    subject. The lookback is capped at `_SUBJECT_LOOKBACK` characters, so this
    one is a correctness guard rather than a timing one — the rewrite from
    `re.sub(r"\\s+\\w+ly$", ...)` to `rpartition` must not change any verdict.
    """

    def test_a_third_party_subject_is_found_behind_an_adverb(self):
        assert _subject_is_someone_else("the system formally recorded ")
        assert _subject_is_someone_else("the scheduler quietly ")

    def test_a_named_subject_is_found_behind_an_adverb(self):
        assert _subject_is_someone_else("Alice promptly ")

    def test_the_agent_speaking_about_itself_is_not_someone_else(self):
        assert not _subject_is_someone_else("I formally ")
        assert not _subject_is_someone_else("we quickly ")

    def test_a_lookback_with_no_subject_in_it_stays_false(self):
        """Dropping the trailing word must not invent a subject behind it."""
        assert not _subject_is_someone_else("and then ")
        assert not _subject_is_someone_else("carefully ")

    def test_no_trailing_word_at_all_is_handled(self):
        assert not _subject_is_someone_else("")
        assert not _subject_is_someone_else("   ")


class TestTheImperativeStepPatternIsLinear:
    """`^\\s*(?:[-*•]|\\d+[.)])?\\s*` splits a leading whitespace run between two
    quantifiers, so a line of N spaces that fails to match costs O(N**2). At
    20,000 spaces the original pattern took ~3 seconds; the rewrite moves the
    inner `\\s*` inside the optional group, leaving exactly one way to consume
    the leading run.
    """

    def test_a_long_whitespace_run_does_not_blow_up(self):
        import time

        from robothor.engine.run_verification import _IMPERATIVE_STEP_RE

        hostile = " " * 20_000 + "!"
        started = time.perf_counter()
        assert _IMPERATIVE_STEP_RE.match(hostile) is None
        assert time.perf_counter() - started < 0.2, (
            "the imperative-step pattern backtracks polynomially on leading whitespace"
        )

    def test_the_shapes_it_exists_to_match_still_match(self):
        from robothor.engine.run_verification import _IMPERATIVE_STEP_RE

        for line in (
            "Confirm the record is updated",
            "  confirm the record",
            "- Confirm the record",
            "* verify the row",
            "• check the flag",
            "1. Set the flag to true",
            "12) update the record",
            "  3.  Look up the contact",
        ):
            assert _IMPERATIVE_STEP_RE.match(line), line

    def test_a_report_of_finished_work_still_does_not_match(self):
        from robothor.engine.run_verification import _IMPERATIVE_STEP_RE

        for line in ("I updated the record", "The record was updated", "Updates were sent"):
            assert _IMPERATIVE_STEP_RE.match(line) is None, line
