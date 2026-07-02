#!/usr/bin/env python3
"""Daily: fit a monotonic calibration map from realized P&L vs model_prob_yes,
write a piecewise-linear lookup table to ``delphi-calibration-shrinkage``.

Cron: 03:45 ET via ``delphi-calibration-shrinkage.timer``.

Method: pool-adjacent-violators (PAV) isotonic regression. We sort live
trades by ``model_prob_yes``, take the realized hit rate per bin, and
enforce monotonicity by merging adjacent violators. The result is a
sequence of (raw_prob, calibrated_prob) knots; predict.py interpolates
linearly between them.

Why: the model can be systematically over/under-confident. Isotonic
regression maps raw probabilities to empirical hit rates without
assuming any parametric form (vs. e.g., Platt scaling which assumes
sigmoid). Standard ML technique.

Safety:
- ``MIN_SAMPLES = 30``: identity passthrough below this. Don't shrink on
  noise.
- ``MAX_DELTA = 0.30``: clip any single-knot calibration shift to this
  absolute value. Isotonic on tiny per-bin samples can swing wildly; this
  prevents one outlier bin from rewriting the whole curve.
- Buckets are quantile-based with ``N_BUCKETS = 10`` (deciles by sample
  count, not by raw_prob value) so each bucket has the same statistical
  weight.

Env: ROBOTHOR_TENANT_ID=delphi (required)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

os.environ["ROBOTHOR_TENANT_ID"] = "delphi"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.delphi.db import DELPHI_TENANT_ID, get_connection  # noqa: E402
from brain.delphi.runtime_state import et_now  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("delphi_calibration_shrinkage")

BLOCK_NAME = "delphi-calibration-shrinkage"
MIN_SAMPLES = 500  # Raised from 30: require adequate sample depth before trusting isotonic fit
MAX_DELTA = 0.30
N_BUCKETS = 10
WINDOW_DAYS = 60  # Rolling 60d window; live + shadow combined for calibration only.


def fetch_trades() -> list[tuple[float, int]]:
    """(model_prob_yes, hit_indicator) for settled trades. hit=1 if we won.

    Uses ``realized_pnl_usd`` as the win signal (Phase 1 truth).
    """
    sql = """
        SELECT
            model_prob_yes,
            CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END AS hit
        FROM delphi_predictions
        WHERE tenant_id = %s
          AND mode = 'live'
          AND decision = 'approved'
          AND realized_pnl_usd IS NOT NULL
          AND model_prob_yes IS NOT NULL
          AND COALESCE(kalshi_settled_at, resolved_at) >= NOW() - make_interval(days => %s)
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (DELPHI_TENANT_ID, WINDOW_DAYS))
        return [(float(r[0]), int(r[1])) for r in cur.fetchall()]


def isotonic_pav(xs_sorted: list[float], ys_sorted: list[float]) -> list[float]:
    """Pool-adjacent-violators regression. Returns calibrated y values
    in sorted-x order, monotonically non-decreasing.

    Standard PAV: walk left-to-right; if the next value violates monotonicity,
    pool the violator with its predecessors (weighted average) until
    monotonicity is restored. Output values match input length.
    """
    n = len(ys_sorted)
    if n == 0:
        return []
    # Each "block" is (start_index, length, mean_y).
    blocks: list[list[float]] = [[i, 1, float(ys_sorted[i])] for i in range(n)]
    i = 1
    while i < len(blocks):
        # Violation: previous block's mean > current's mean.
        if blocks[i - 1][2] > blocks[i][2]:
            # Pool: merge into one weighted block.
            prev = blocks[i - 1]
            cur = blocks[i]
            new_len = prev[1] + cur[1]
            new_mean = (prev[1] * prev[2] + cur[1] * cur[2]) / new_len
            blocks[i - 1] = [prev[0], new_len, new_mean]
            blocks.pop(i)
            # Move back to recheck previous boundary.
            if i > 1:
                i -= 1
        else:
            i += 1
    # Expand blocks back to per-sample fitted values.
    out: list[float] = [0.0] * n
    for start, length, mean in blocks:
        for k in range(int(length)):
            out[int(start) + k] = mean
    return out


def build_knots(trades: list[tuple[float, int]]) -> list[list[float]]:
    """Return the (raw_prob, calibrated_prob) knot list for piecewise interp.

    Implementation:
      1. Sort trades by raw model_prob_yes.
      2. Run PAV to get monotone calibrated values per sample.
      3. Bucket into N_BUCKETS roughly equal-count deciles; one knot per bucket
         at (median_raw, mean_calibrated).
      4. Clip any knot's |calib - raw| > MAX_DELTA.
      5. Always pin (0, 0) and (1, 1) as endpoints so out-of-range raw probs
         interpolate to identity.
    """
    if not trades:
        return [[0.0, 0.0], [1.0, 1.0]]
    sorted_trades = sorted(trades, key=lambda t: t[0])
    xs = [t[0] for t in sorted_trades]
    ys = [float(t[1]) for t in sorted_trades]
    fitted = isotonic_pav(xs, ys)

    # Decile bucketing.
    n = len(xs)
    bucket_size = max(1, n // N_BUCKETS)
    knots: list[list[float]] = []
    seen_raw: set[float] = set()
    for b in range(N_BUCKETS):
        start = b * bucket_size
        end = (b + 1) * bucket_size if b < N_BUCKETS - 1 else n
        if start >= n:
            break
        bucket_xs = xs[start:end]
        bucket_ys = fitted[start:end]
        if not bucket_xs:
            continue
        median_raw = bucket_xs[len(bucket_xs) // 2]
        mean_cal = sum(bucket_ys) / len(bucket_ys)
        # Clip large per-knot deltas.
        delta = mean_cal - median_raw
        if abs(delta) > MAX_DELTA:
            mean_cal = median_raw + (MAX_DELTA if delta > 0 else -MAX_DELTA)
        # Avoid duplicate raw values (would break linear interp).
        raw = median_raw
        while raw in seen_raw:
            raw += 1e-6
        seen_raw.add(raw)
        knots.append([round(raw, 4), round(mean_cal, 4)])

    # Pin endpoints to keep extrapolation identity.
    if not knots or knots[0][0] > 0.0:
        knots.insert(0, [0.0, 0.0])
    if knots[-1][0] < 1.0:
        knots.append([1.0, 1.0])
    return knots


def write_block(payload: dict) -> None:
    sql = """
        INSERT INTO agent_memory_blocks (block_name, block_type, content, max_chars, tenant_id)
        VALUES (%s, 'system', %s, 4000, %s)
        ON CONFLICT (tenant_id, block_name) DO UPDATE
            SET content = EXCLUDED.content,
                last_written_at = NOW(),
                write_count = agent_memory_blocks.write_count + 1
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (BLOCK_NAME, json.dumps(payload), DELPHI_TENANT_ID))
        conn.commit()


def main() -> int:
    trades = fetch_trades()
    n = len(trades)
    logger.info("Fetched %d settled live trades over last %dd", n, WINDOW_DAYS)
    if n < MIN_SAMPLES:
        logger.warning("Below MIN_SAMPLES=%d — writing identity fallback", MIN_SAMPLES)
        write_block(
            {
                "computed_at": et_now().isoformat(timespec="seconds"),
                "n_samples": n,
                "knots": [[0.0, 0.0], [1.0, 1.0]],
                "fallback": "identity",
                "reason": f"n<{MIN_SAMPLES}",
            }
        )
        return 0
    knots = build_knots(trades)

    # Flat-region detection: if more than 30% of interior knots share the same
    # calibrated value, the fit has collapsed to a constant — no discriminative
    # signal. Fall back to identity rather than write garbage.
    interior = [cal for _raw, cal in knots if _raw not in (0.0, 1.0)]
    if interior:
        modal_val = max(set(interior), key=interior.count)
        flat_fraction = interior.count(modal_val) / len(interior)
    else:
        flat_fraction = 0.0

    if flat_fraction > 0.30:
        logger.warning(
            "Calibration curve degenerate: flat_fraction=%.2f (modal=%.4f). "
            "Writing identity fallback.",
            flat_fraction,
            modal_val if interior else 0.0,
        )
        write_block(
            {
                "computed_at": et_now().isoformat(timespec="seconds"),
                "n_samples": n,
                "knots": [[0.0, 0.0], [1.0, 1.0]],
                "fallback": "identity",
                "reason": f"flat_fraction={flat_fraction:.2f}>0.30",
            }
        )
        return 0

    write_block(
        {
            "computed_at": et_now().isoformat(timespec="seconds"),
            "n_samples": n,
            "knots": knots,
            "fallback": "isotonic",
        }
    )
    logger.info(
        "Wrote calibration map with %d knots (flat_fraction=%.2f)", len(knots), flat_fraction
    )
    for raw, cal in knots:
        logger.info("  %.4f → %.4f  (delta %+.4f)", raw, cal, cal - raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
