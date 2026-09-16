"""The extractor reads attacker-controlled text, so it must run in linear time.

The task statement is user input. It reaches `extract_contract` unchanged, and
until 2026-09-16 four of the regexes it runs there were quadratic in a run of
spaces or tabs: CodeQL flagged `py/polynomial-redos` on `_BULLET_RE`,
`_COLUMNS_RE`, `_HEADING_RE` and `_SORT_RE`. Measured on the code as it stood:

* `_SORT_RE` on ``"sorted by `x` asc" + " " * 20_000`` — 10.1 s.
* `_COLUMNS_RE` on ``"columns" + " " * 20_000`` — 0.42 s.
* `_BULLET_RE` on ``"*" + "\\t" * 20_000 + "\\nx"`` — over 120 s; killed.

Each shares one shape: two quantifiers that can match the same character
sitting next to each other, so a run of n whitespace characters has O(n²)
ways to be split between them and the engine tries them all before failing.
The fix is structural — every `\\s*`/`\\s+` is now followed by an atom that
cannot match whitespace — plus a declared cap on how much text is scanned at
all. This file is the regression: it drives the whole extractor and each
individual regex with its own worst case under a wall-clock budget.

A budget test cannot be written in-process. A regex runs inside CPython's C
matcher, which does not check for signals, so `signal.alarm` will not
interrupt it and a regression would hang the suite forever rather than fail
it. The work therefore runs in a child process that gets terminated when the
budget expires.
"""

from __future__ import annotations

import multiprocessing

import pytest

#: Wall-clock allowed for one extraction of the adversarial text. Generous by
#: two orders of magnitude against the linear cost (~5 ms) and still far below
#: the 10 s the old `_SORT_RE` alone took on a fifth of the input.
BUDGET_SECONDS = 1.0

#: Long enough that all four attacks sit inside `MAX_SCAN_CHARS` together.
ATTACK_LEN = 12_000

#: The per-regex probes are not capped by anything, so they go bigger: at this
#: length every one of the four took over a second before the rewrite.
REGEX_ATTACK_LEN = 50_000


def _adversarial_text() -> str:
    """~100 KB of the shapes each flagged regex is worst at, interleaved.

    Every attack sits inside the first `MAX_SCAN_CHARS` so the cap cannot be
    what makes this test pass.
    """
    block = (
        "Write the table to results/out.tsv with columns" + " " * ATTACK_LEN + "\n"
        "*" + "\t" * ATTACK_LEN + "x\n"
        "#" + " " * ATTACK_LEN + "\n"
        "sorted by `score` ascending" + " " * ATTACK_LEN + ",\n"
    )
    text = block
    while len(text) < 100 * 1024:
        text += block
    return text


def _run_extract(payload: str) -> None:
    from robothor.engine.deliverable_extract import extract_contract

    extract_contract(payload)


def _run_regex(name: str, payload: str) -> None:
    from robothor.engine import deliverable_extract as extract

    pattern = getattr(extract, name)
    if pattern.groups and name == "_BULLET_RE":
        pattern.match(payload)
    else:
        list(pattern.finditer(payload))


def _finishes_within(target, args, budget: float) -> bool:
    """True if `target(*args)` completed inside `budget` seconds.

    The child is killed on overrun, which is the only way a hung matcher can
    be turned into a test failure rather than a hung suite.
    """
    child = multiprocessing.get_context("fork").Process(target=target, args=args)
    child.start()
    child.join(budget)
    if child.is_alive():
        child.terminate()
        child.join(5)
        return False
    return child.exitcode == 0


class TestTheExtractorIsLinearInItsInput:
    def test_a_100kb_adversarial_task_extracts_inside_a_second(self):
        text = _adversarial_text()
        assert len(text) >= 100 * 1024
        assert _finishes_within(_run_extract, (text,), BUDGET_SECONDS), (
            "extract_contract did not finish within "
            f"{BUDGET_SECONDS}s on {len(text)} chars of adversarial whitespace — "
            "a quantifier pair that can match the same character has come back."
        )

    @pytest.mark.parametrize(
        ("name", "payload"),
        [
            # The `\n` matters: `_BULLET_RE` is fed one line at a time today, so
            # this input is unreachable through `extract_contract`. The regex is
            # still a landmine for the next caller, and CodeQL reads it as one.
            ("_BULLET_RE", "*" + "\t" * REGEX_ATTACK_LEN + "\nx"),
            ("_COLUMNS_RE", "columns" + " " * REGEX_ATTACK_LEN),
            ("_HEADING_RE", "#" + " " * REGEX_ATTACK_LEN + "\n#" + " " * REGEX_ATTACK_LEN),
            ("_SORT_RE", "sorted by `x` asc" + " " * REGEX_ATTACK_LEN + ","),
        ],
        ids=["bullet", "columns", "heading", "sort"],
    )
    def test_each_flagged_regex_survives_its_own_worst_case(self, name, payload):
        assert _finishes_within(_run_regex, (name, payload), BUDGET_SECONDS), (
            f"{name} did not finish within {BUDGET_SECONDS}s on {len(payload)} chars"
        )


class TestTheScanIsBounded:
    def test_the_cap_is_declared_and_the_scan_stops_there(self):
        from robothor.engine.deliverable_extract import MAX_SCAN_CHARS, extract_contract

        assert MAX_SCAN_CHARS == 64 * 1024

        spec = "Save the report to reports/summary.md.\n"
        beyond = " " * MAX_SCAN_CHARS + spec
        assert extract_contract(beyond).items == (), (
            "a requirement past the cap must not be read — worst-case work has "
            "to be bounded by the cap, not by the length of the task"
        )

    def test_a_requirement_inside_the_cap_still_reads(self):
        from robothor.engine.deliverable_extract import extract_contract

        spec = "Save the report to reports/summary.md.\n"
        items = extract_contract(spec + "\n" + "filler. " * 2_000).items
        assert [getattr(i, "path", None) for i in items] == ["reports/summary.md"]
