"""The runner's ``SETUP <n>ms agent=... trigger=...`` line (robothor/engine/runner.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass

SETUP_RE = re.compile(
    r"SETUP (?P<ms>\d+)ms agent=(?P<agent>\S+) trigger=(?P<trigger>\S+)"
    r" warmup=(?P<warmup>\S+) cached_prompt=(?P<cached>\S+)"
)


@dataclass(frozen=True)
class SetupRecord:
    ms: int
    agent: str
    trigger: str
    warmup: str
    cached_prompt: str


def parse_setup_line(line: str) -> SetupRecord | None:
    match = SETUP_RE.search(line)
    if not match:
        return None
    return SetupRecord(
        ms=int(match["ms"]),
        agent=match["agent"],
        trigger=match["trigger"],
        warmup=match["warmup"],
        cached_prompt=match["cached"],
    )


def parse_setup_lines(text: str) -> list[SetupRecord]:
    """Every SETUP record in ``text``, in order."""
    return [r for r in (parse_setup_line(line) for line in text.splitlines()) if r]
