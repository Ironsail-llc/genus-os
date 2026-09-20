"""Deterministic uncertainty estimates; failed and timed-out samples stay in the population."""

from __future__ import annotations

import math
import random


def quantile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def summary(values):
    if not values:
        return {"samples": 0, "p50": None, "p95": None, "p95_ci95": None}
    rng = random.Random(20260920)
    estimates = [quantile(rng.choices(values, k=len(values)), 0.95) for _ in range(1000)]
    return {
        "samples": len(values),
        "p50": quantile(values, 0.5),
        "p95": quantile(values, 0.95),
        "p95_ci95": [quantile(estimates, 0.025), quantile(estimates, 0.975)],
    }
