"""p50/p95 per metric over a run's turns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize(turns: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{metric: {p50, p95, n}}`` over every numeric value the turns recorded."""
    columns: dict[str, list[float]] = {}
    for turn in turns:
        for metric, value in turn.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            columns.setdefault(metric, []).append(float(value))
    return {
        metric: {"p50": percentile(values, 50), "p95": percentile(values, 95), "n": len(values)}
        for metric, values in sorted(columns.items())
    }
