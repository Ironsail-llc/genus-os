"""Read the rotation's ledger and say what it actually establishes.

    python -m bench.wildclaw.ledger [--ledger PATH]

Measured 2026-08-26: the nightly rotation scored Productivity Flow at 28.3%
unattended, one day after a hand-driven run of the same category — same
tasks, same model, same graders — scored 37.6%. Nine points apart. The gap
the whole OpenClaw comparison turns on is 1.3.

So reading one ledger line and concluding anything is the same error as
trusting a green test over a live probe: a number that looks like an answer
and is not. A whole campaign was spent chasing a difference smaller than the
noise around it.

This reports, per category, the mean across every run the rotation has done,
the observed spread, and whether the distance from the published baseline is
larger than that spread. When it is not, the verdict is "too close to call"
— and printing that instead of a number is the entire point of the file.

Deliberately no statistics beyond mean and range. With three or four samples
a confidence interval implies a precision the data does not have, and this
project has enough experience of controls that looked more certain than they
were.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Where the nightly rotation writes, matching robothor.env's WILDCLAW_OUT.
_DEFAULT_LEDGER = "ledger.jsonl"


@dataclass(frozen=True)
class CategoryStanding:
    """What the ledger establishes about one category, and how firmly."""

    category: str
    runs: int
    mean: float
    spread: float
    baseline: float | None
    verdict: str


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Every entry the rotation has written. A bad line is skipped, not fatal.

    A ledger is append-only history; one malformed line must not make the
    whole record unreadable.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("category"):
            rows.append(entry)
    return rows


def _verdict(runs: int, mean: float, spread: float, baseline: float | None) -> str:
    """Whether the ledger can call this category yet.

    The bar is that the distance from the baseline must exceed the spread we
    have actually observed. Anything closer than the noise is not a finding.
    """
    if baseline is None:
        return "no baseline"
    if runs < 2:
        # With one sample there is no observed spread at all. Calling a
        # winner from it is precisely the mistake this module exists to stop.
        return "one run — not yet conclusive"
    gap = mean - baseline
    if abs(gap) <= spread:
        return "too close to call"
    return "ahead" if gap > 0 else "behind"


def summarize(rows: list[dict[str, Any]]) -> list[CategoryStanding]:
    """Group by category and judge each against its own observed spread.

    Ordered so anything the ledger can actually call comes first — the
    uncertain rows are the ones needing more runs, not more attention.
    """
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_category.setdefault(str(row["category"]), []).append(row)

    standings: list[CategoryStanding] = []
    for category, entries in by_category.items():
        means = [float(e.get("mean") or 0.0) for e in entries]
        mean = sum(means) / len(means)
        spread = max(means) - min(means)
        baselines: list[float] = [
            float(b) for e in entries if (b := e.get("baseline_mean")) is not None
        ]
        baseline = baselines[-1] if baselines else None
        standings.append(
            CategoryStanding(
                category=category,
                runs=len(entries),
                mean=mean,
                spread=spread,
                baseline=baseline,
                verdict=_verdict(len(entries), mean, spread, baseline),
            )
        )

    decisive = {"ahead", "behind"}
    return sorted(
        standings,
        key=lambda s: (s.verdict not in decisive, s.category),
    )


def render(standings: list[CategoryStanding]) -> str:
    """A plain-text standing an operator can read without context."""
    if not standings:
        return (
            "The ledger is empty. The nightly rotation writes one entry per "
            "run; give it a few nights.\n"
        )
    width = max(len(s.category) for s in standings)
    lines = [
        f"{'category'.ljust(width)}  runs   mean   spread   baseline   verdict",
        f"{'-' * width}  ----   ----   ------   --------   -------",
    ]
    for s in standings:
        base = f"{s.baseline * 100:7.1f}" if s.baseline is not None else "      —"
        lines.append(
            f"{s.category.ljust(width)}  {s.runs:>4}  {s.mean * 100:5.1f}  "
            f"{s.spread * 100:6.1f}   {base}   {s.verdict}"
        )
    unresolved = [s for s in standings if s.verdict == "too close to call"]
    if unresolved:
        lines.append("")
        lines.append(
            "'Too close to call' means the distance from the baseline is inside "
            "the spread these runs have actually shown. More runs, not a "
            "different conclusion."
        )
    lines.append("")
    lines.append(
        f"{sum(s.runs for s in standings)} runs recorded across {len(standings)} categories."
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="path to ledger.jsonl (default: $WILDCLAW_OUT/ledger.jsonl)",
    )
    args = ap.parse_args()
    path = args.ledger
    if path is None:
        out = os.environ.get("WILDCLAW_OUT", "")
        if not out:
            print("WILDCLAW_OUT is not set and no --ledger was given")
            return 2
        path = Path(out) / _DEFAULT_LEDGER
    print(render(summarize(read_ledger(path))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
