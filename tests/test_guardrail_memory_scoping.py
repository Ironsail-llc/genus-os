"""The vacuous-scoping alarm must actually fire.

A guardrail that has only ever been observed staying quiet is indistinguishable
from one that cannot fire at all. This codebase has a standing lesson about
exactly that, so the condition is pure and tested on both sides.

Context: ROBOTHOR_DATA_SCOPING=enforce restricts memory reads to rows whose
person_id matches the caller or is NULL, but nothing writes
memory_facts.person_id — 0 of the last 5,521 facts carried one. That is
harmless while every tenant_users row is privileged, and stops being harmless
the moment a lesser role is granted.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "guardrail_watch", Path(__file__).resolve().parents[1] / "scripts" / "guardrail_watch.py"
)
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)


@pytest.mark.parametrize(
    "non_privileged,linked,expected,why",
    [
        (0, 0, False, "todays state: only owners exist, so nothing is mis-scoped yet"),
        (1, 0, True, "the alarm case: a lesser role exists and no fact can be attributed"),
        (5, 0, True, "several lesser roles, still nothing to filter on"),
        (1, 42, False, "a writer exists, so the predicate can actually bind"),
        (0, 42, False, "linked facts but no lesser roles — nothing to warn about"),
    ],
)
def test_alarm_condition(non_privileged, linked, expected, why):
    assert gw.scoping_is_vacuous(non_privileged, linked) is expected, why


def test_alarm_is_not_permanently_silent():
    """Guard against a future edit that makes the condition unreachable."""
    assert gw.scoping_is_vacuous(1, 0) is True, (
        "the vacuous-scoping alarm can no longer fire under any input — "
        "it has become decoration"
    )
