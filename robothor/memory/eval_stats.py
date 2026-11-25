"""Paired significance testing for memory-eval comparisons.

Retrieval decisions were previously made by eyeballing two aggregate
percentages. On a 41-question set a 2-point difference is a single flipped
case, which is indistinguishable from reranker noise — the reranker is a model
and borderline cases move between runs.

McNemar's exact test is the right instrument because both arms answer the
*same* questions, so only the pairs where they disagree carry information. It
also makes the limits of a small suite explicit: with all disagreement in one
direction you need at least 6 discordant pairs to reach p < 0.05, so a stratum
of 4 cases cannot produce a significant result however lopsided it looks. That
arithmetic is why the suite has to grow before it can gate anything.

Stdlib only — scipy is not a dependency here and numpy buys nothing at this
size.
"""

from __future__ import annotations

import math
from typing import Any


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts ``b`` and ``c``.

    ``b`` improvements, ``c`` regressions. Concordant pairs are deliberately not
    arguments: they carry no information about a difference between the arms.

    Exact binomial rather than the chi-squared approximation, which is unreliable
    below roughly 25 discordant pairs — the regime every stratum here lives in.
    """
    n = b + c
    if n == 0:
        return 1.0
    # P(X <= min) + P(X >= max) under Binomial(n, 0.5); by symmetry that is
    # twice the smaller tail, clamped at 1.
    k = min(b, c)
    tail = float(sum(math.comb(n, i) for i in range(k + 1))) / (1 << n)
    return min(1.0, 2.0 * tail)


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials.

    Preferred over the normal approximation because it stays inside [0, 1] and
    behaves at the extremes — a stratum that scores 5/5 still gets an honest
    lower bound instead of a meaningless zero-width interval.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def paired_report(
    before: dict[str, bool],
    after: dict[str, bool],
    strata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare two arms over the questions both answered.

    Questions present in only one arm are excluded rather than scored as
    failures: an arm that errored on a case has produced no evidence about it,
    and counting that as a regression would manufacture a difference.

    Returns counts, delta, the exact p-value, and a per-stratum breakdown so the
    do-no-harm rule ("no stratum regressing at its own p < 0.20") can be applied
    without recomputing anything.
    """
    shared = sorted(set(before) & set(after))
    improved = sum(1 for q in shared if after[q] and not before[q])
    regressed = sum(1 for q in shared if before[q] and not after[q])
    n = len(shared)

    report: dict[str, Any] = {
        "n": n,
        "improved": improved,
        "regressed": regressed,
        "before_passed": sum(1 for q in shared if before[q]),
        "after_passed": sum(1 for q in shared if after[q]),
        "delta": 0.0 if n == 0 else (improved - regressed) / n,
        "p": mcnemar_exact(improved, regressed),
        "per_stratum": {},
    }

    if strata:
        buckets: dict[str, list[str]] = {}
        for q in shared:
            buckets.setdefault(strata.get(q, "unknown"), []).append(q)
        for name, qs in sorted(buckets.items()):
            imp = sum(1 for q in qs if after[q] and not before[q])
            reg = sum(1 for q in qs if before[q] and not after[q])
            passed = sum(1 for q in qs if after[q])
            report["per_stratum"][name] = {
                "n": len(qs),
                "improved": imp,
                "regressed": reg,
                "p": mcnemar_exact(imp, reg),
                "wilson": wilson_interval(passed, len(qs)),
            }
    return report


def promotion_verdict(report: dict[str, Any], stratum_alpha: float = 0.20) -> tuple[bool, str]:
    """Apply the standing promotion rule to a paired report.

    Promote only when the change is significantly positive overall *and* no
    stratum is significantly worse. The one-sided stratum guard is deliberately
    loose (0.20): a regression should have to clear a low bar to block, not a
    high one, because the cost of shipping a quiet regression is worse than the
    cost of one more measurement round.
    """
    if report["n"] == 0:
        return False, "no shared questions — nothing was measured"
    if report["delta"] <= 0:
        return False, f"delta {report['delta']:+.3f} is not positive"
    if report["p"] >= 0.05:
        return False, (
            f"p={report['p']:.3f} — not significant "
            f"({report['improved']}↑/{report['regressed']}↓ discordant pairs; "
            f"6 one-sided are needed for p<0.05)"
        )
    for name, s in report.get("per_stratum", {}).items():
        if s["regressed"] > s["improved"] and s["p"] < stratum_alpha:
            return False, (
                f"stratum {name!r} regressed ({s['improved']}↑/{s['regressed']}↓, p={s['p']:.3f})"
            )
    return True, (f"delta {report['delta']:+.3f}, p={report['p']:.3f}, no stratum regression")


# --------------------------------------------------------------------------- #
# Answer-arm additions: non-inferiority, power, run-to-run spread
# --------------------------------------------------------------------------- #

_Z_95 = 1.959964
_Z_POWER_80 = 0.841621


def paired_difference_ci(
    before: dict[str, bool], after: dict[str, bool], z: float = _Z_95
) -> tuple[float, float, float]:
    """Accuracy difference ``after - before`` over shared questions, with a
    Wald interval for paired proportions.

    With ``b`` improvements, ``c`` regressions and ``n`` shared questions:
    ``d = (b - c) / n`` and ``var = ((b + c) - (b - c)^2 / n) / n^2``.
    Returns ``(d, lower, upper)``; all zero when nothing is shared.
    """
    shared = sorted(set(before) & set(after))
    n = len(shared)
    if n == 0:
        return (0.0, 0.0, 0.0)
    b = sum(1 for q in shared if after[q] and not before[q])
    c = sum(1 for q in shared if before[q] and not after[q])
    diff = (b - c) / n
    var = max(0.0, ((b + c) - (b - c) ** 2 / n) / (n * n))
    half = z * math.sqrt(var)
    if half == 0.0:
        return (diff, diff, diff)
    return (diff, diff - half, diff + half)


def non_inferiority(
    reference: dict[str, bool], candidate: dict[str, bool], margin: float = 0.05
) -> dict[str, Any]:
    """Is ``candidate`` no worse than ``reference`` by more than ``margin``?

    Non-inferior when the lower bound of the 95% interval on the paired
    accuracy difference (candidate minus reference) is above ``-margin``.
    """
    diff, lower, upper = paired_difference_ci(reference, candidate)
    return {
        "diff": diff,
        "lower": lower,
        "upper": upper,
        "margin": margin,
        "n": len(set(reference) & set(candidate)),
        "non_inferior": lower > -margin,
    }


def minimum_detectable_effect(
    n: int, p: float = 0.5, z_alpha: float = _Z_95, z_power: float = _Z_POWER_80
) -> float:
    """Smallest accuracy difference a stratum of ``n`` cases can detect.

    Two-sided alpha 0.05, power 0.8, for two arms at accuracy ``p``:
    ``(z_a + z_b) * sqrt(2 p (1 - p) / n)``. ``p`` is clamped to [0.05, 0.95]
    so a perfect stratum still reports a non-zero floor. Stated per stratum so
    a difference smaller than this is never read as a result.
    """
    if n <= 0:
        return 1.0
    p = min(max(p, 0.05), 0.95)
    return min(1.0, (z_alpha + z_power) * math.sqrt(2 * p * (1 - p) / n))


def run_spread(accuracies: list[float]) -> dict[str, Any]:
    """Min, max, range and sample standard deviation across repeated runs."""
    if not accuracies:
        return {"runs": 0, "min": 0.0, "max": 0.0, "range": 0.0, "stdev": 0.0}
    lo, hi = min(accuracies), max(accuracies)
    n = len(accuracies)
    stdev = 0.0
    if n > 1:
        mean = sum(accuracies) / n
        stdev = math.sqrt(sum((a - mean) ** 2 for a in accuracies) / (n - 1))
    return {"runs": n, "min": lo, "max": hi, "range": hi - lo, "stdev": stdev}


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])
