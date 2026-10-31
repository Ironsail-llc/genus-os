"""Compare a run's summary with a committed baseline.

Every metric the benchmark records is lower-is-better, so a regression is a
p50 or p95 more than ``threshold`` above the baseline's and more than
``min_delta`` above it in absolute terms (a 0 -> 0.5 ms move is an infinite
ratio and still noise).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Committed in place of a baseline until the first real one is recorded.
EMPTY_BASELINE: dict[str, Any] = {
    "empty": True,
    "note": (
        "No baseline recorded yet. Record one on the reference machine with "
        "`python -m bench.turn ... --update-baseline` and commit this file."
    ),
    "summary": {},
}

STATS = ("p50", "p95")


@dataclass(frozen=True)
class Regression:
    metric: str
    stat: str
    baseline: float
    current: float

    @property
    def change(self) -> float:
        return (self.current - self.baseline) / self.baseline if self.baseline else float("inf")


@dataclass
class Comparison:
    threshold: float
    no_baseline: bool = False
    regressions: list[Regression] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def regressed(self) -> bool:
        return bool(self.regressions)

    @property
    def exit_code(self) -> int:
        return 1 if self.regressed else 0

    @property
    def message(self) -> str:
        return "\n".join(self.lines)


def is_empty_baseline(baseline: dict[str, Any] | None) -> bool:
    return not baseline or bool(baseline.get("empty")) or not baseline.get("summary")


def compare(
    baseline: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    threshold: float = 0.10,
    min_delta: float = 1.0,
) -> Comparison:
    outcome = Comparison(threshold=threshold)
    if is_empty_baseline(baseline):
        outcome.no_baseline = True
        outcome.lines.append(
            "no baseline recorded: comparison skipped. Record one with "
            "`python -m bench.turn ... --update-baseline` and commit it."
        )
        return outcome

    assert baseline is not None
    base = baseline.get("summary") or {}
    now = current.get("summary") or {}
    for metric in sorted(set(base) | set(now)):
        if metric not in base or metric not in now:
            outcome.skipped.append(metric)
            outcome.lines.append(
                f"  {metric:<22} skipped (missing from {'baseline' if metric not in base else 'this run'})"
            )
            continue
        for stat in STATS:
            b, c = base[metric].get(stat), now[metric].get(stat)
            if b is None or c is None:
                continue
            limit = b * (1.0 + threshold)
            bad = c > limit and (c - b) > min_delta
            verdict = "REGRESSION" if bad else "ok"
            outcome.lines.append(f"  {metric:<22} {stat} {b:>10.1f} -> {c:>10.1f}  {verdict}")
            if bad:
                outcome.regressions.append(Regression(metric, stat, float(b), float(c)))
    head = (
        f"{len(outcome.regressions)} regression(s) over {threshold:.0%}"
        if outcome.regressed
        else f"within threshold ({threshold:.0%})"
    )
    outcome.lines.insert(0, head)
    return outcome
