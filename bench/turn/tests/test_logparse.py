"""The runner's SETUP log line becomes a number."""

from __future__ import annotations

from bench.turn.logparse import parse_setup_lines

LOG = """\
2026-10-31 10:00:00,001 INFO robothor.engine.runner: starting run
2026-10-31 10:00:00,413 INFO robothor.engine.runner: SETUP 412ms agent=main trigger=telegram warmup=full cached_prompt=miss
2026-10-31 10:00:01,000 INFO robothor.engine.runner: TOOLSET 3ms
SETUP 9ms agent=example-agent trigger=webchat warmup=none cached_prompt=hit
"""


def test_setup_lines_are_parsed_and_others_ignored() -> None:
    records = parse_setup_lines(LOG)
    assert [r.ms for r in records] == [412, 9]
    assert records[0].agent == "main"
    assert records[0].trigger == "telegram"
    assert records[0].warmup == "full"
    assert records[0].cached_prompt == "miss"
    assert records[1].agent == "example-agent"
    assert records[1].cached_prompt == "hit"


def test_no_setup_line_gives_an_empty_list() -> None:
    assert parse_setup_lines("nothing here\n") == []
