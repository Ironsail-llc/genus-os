"""Self-consistency for recurring operator-facing metrics.

The 2026-08-22 morning briefing closed with ``Fleet health: 52.8% (↓0.5pp WoW)``
while its own previous briefings had published 48.6% and 57.8%. The real change
was +4.2pp; the delivered claim was a half-point fall. Neither the value nor the
trend had a source: the run's tool output carried no fleet-health field, was
truncated at 9,749 chars, and no historical query ran.

Scope is deliberately narrow. A broader "does this number appear in the run's
tool outputs" check was built first and rejected on measurement: it MISSED this
case (52.8 occurs by coincidence inside a large JSON blob) and flagged 15 of 21
legitimate numbers on other days. A control with that error rate certifies
nothing, and shipping one is the exact failure this codebase keeps finding.

What is unambiguous is self-consistency. When an agent publishes a labelled
metric together with a direction, and it published the same label before, the
direction must agree with its own record. That needs no tool trace and no
arithmetic guesswork — only the agent's own words, twice — and it catches the
operator-facing failure that matters: a confident trend contradicting the record.

Silence is the default. No previous publication, or no claimed trend, means no
finding: absence of history is not evidence of a lie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: How far a claimed delta may sit from the computed one before it is a
#: contradiction. One decimal place of rounding on each end is not a lie.
DELTA_TOLERANCE = 0.25

#: ``Label: 12.3% (↓0.5pp WoW)`` — the label must be a short leading phrase on
#: its own, so ordinary prose containing a percentage is not mistaken for a
#: published metric.
_METRIC = re.compile(
    r"""
    (?:^|\n)\s*\**\s*
    (?P<label>[A-Za-z][A-Za-z /-]{2,40}?)
    \s*\**\s*:\s*\**\s*
    (?P<value>\d+(?:\.\d+)?)\s*%
    (?P<trend>\s*\(\s*[^)]{0,60}\))?
    """,
    re.VERBOSE,
)

_DOWN = re.compile(r"[↓▼]|\b(?:down|fell|dropped|decreas\w*|lower)\b", re.IGNORECASE)
_UP = re.compile(r"[↑▲]|\b(?:up|rose|climbed|increas\w*|higher)\b", re.IGNORECASE)
_MAGNITUDE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:pp|%|points?|pts?)", re.IGNORECASE)


@dataclass(frozen=True)
class MetricClaim:
    """A labelled percentage an agent published, with any claimed direction."""

    label: str
    value: float
    delta: float | None
    phrase: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "delta": self.delta,
            "phrase": self.phrase[:200],
        }


@dataclass(frozen=True)
class TrendViolation:
    """A published direction that contradicts the agent's own previous number."""

    label: str
    claimed_delta: float
    actual_delta: float
    previous_value: float
    current_value: float
    detail: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "claimed_delta": float(self.claimed_delta),
            "actual_delta": float(self.actual_delta),
            "previous_value": float(self.previous_value),
            "current_value": float(self.current_value),
            "detail": self.detail,
        }


def _parse_trend(raw: str | None) -> float | None:
    """Signed magnitude from a trend clause, or None when it carries no number."""
    if not raw:
        return None
    magnitude = _MAGNITUDE.search(raw)
    if not magnitude:
        return None
    size = float(magnitude.group(1))
    if _DOWN.search(raw):
        return -size
    if _UP.search(raw):
        return size
    return None


def extract_metric_claims(text: str | None) -> list[MetricClaim]:
    """Every ``Label: N%`` an agent published, with its claimed direction."""
    if not text:
        return []
    claims: list[MetricClaim] = []
    for match in _METRIC.finditer(text):
        label = " ".join(match.group("label").split()).strip(" *-/").lower()
        if not label:
            continue
        claims.append(
            MetricClaim(
                label=label,
                value=float(match.group("value")),
                delta=_parse_trend(match.group("trend")),
                phrase=match.group(0).strip(),
            )
        )
    return claims


def check_trend_consistency(
    current: list[MetricClaim], previous: list[MetricClaim]
) -> list[TrendViolation]:
    """Directions that contradict the same agent's previous publication.

    Only labels present in BOTH, and only where the current claim states a
    direction. Everything else is silence by design.
    """
    prior = {c.label: c for c in previous}
    violations: list[TrendViolation] = []
    for claim in current:
        if claim.delta is None:
            continue
        was = prior.get(claim.label)
        if was is None:
            continue
        actual = round(claim.value - was.value, 4)
        if abs(actual - claim.delta) <= DELTA_TOLERANCE:
            continue
        violations.append(
            TrendViolation(
                label=claim.label,
                claimed_delta=claim.delta,
                actual_delta=actual,
                previous_value=was.value,
                current_value=claim.value,
                detail=(
                    f"published {claim.delta:+.1f}pp but its own previous figure "
                    f"({was.value:g}% → {claim.value:g}%) is {actual:+.1f}pp"
                ),
            )
        )
    return violations
