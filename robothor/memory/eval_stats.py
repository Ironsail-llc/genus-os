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
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
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
