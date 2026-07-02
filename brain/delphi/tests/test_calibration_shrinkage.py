"""Tests for scripts/delphi_calibration_shrinkage.py — isotonic regression
correctness, identity fallback at small n, max-delta clipping.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def calib_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "delphi_calibration_shrinkage.py"
    spec = importlib.util.spec_from_file_location("delphi_calibration_shrinkage", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── PAV correctness ──────────────────────────────────────────────────────────


def test_pav_already_monotone_passthrough(calib_module):
    """If input is already non-decreasing, PAV returns it unchanged."""
    xs = [0.1, 0.3, 0.5, 0.7, 0.9]
    ys = [0.1, 0.3, 0.5, 0.7, 0.9]
    out = calib_module.isotonic_pav(xs, ys)
    assert out == pytest.approx(ys)


def test_pav_pools_violators(calib_module):
    """[1,0] pools to [0.5, 0.5] — classic PAV merge."""
    xs = [0.1, 0.2]
    ys = [1.0, 0.0]
    out = calib_module.isotonic_pav(xs, ys)
    assert out == pytest.approx([0.5, 0.5])


def test_pav_full_pool(calib_module):
    """[1, 0, 0] → all three pool to mean 1/3."""
    xs = [0.1, 0.2, 0.3]
    ys = [1.0, 0.0, 0.0]
    out = calib_module.isotonic_pav(xs, ys)
    assert out == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_pav_partial_pool(calib_module):
    """[0, 1, 0] → keep first 0, pool last two: [0, 0.5, 0.5]."""
    out = calib_module.isotonic_pav([0.1, 0.2, 0.3], [0.0, 1.0, 0.0])
    assert out == pytest.approx([0.0, 0.5, 0.5])


def test_pav_empty(calib_module):
    assert calib_module.isotonic_pav([], []) == []


# ─── build_knots ──────────────────────────────────────────────────────────────


def test_build_knots_pins_endpoints(calib_module):
    """Even with trades not at the extremes, knots include (0,0) and (1,1)."""
    trades = [(0.30, 1), (0.50, 0), (0.70, 1)]
    knots = calib_module.build_knots(trades)
    assert knots[0] == [0.0, 0.0]
    assert knots[-1] == [1.0, 1.0]


def test_build_knots_clips_large_deltas(calib_module):
    """A bucket whose calibration would shift > MAX_DELTA gets clipped."""
    # All raw_prob = 0.85 but realized hit rate = 0% → calibrated would be 0.0.
    # MAX_DELTA = 0.30 → clipped to 0.85 - 0.30 = 0.55.
    trades = [(0.85 + i * 0.001, 0) for i in range(50)]
    knots = calib_module.build_knots(trades)
    # Find the knot near 0.85.
    relevant = [k for k in knots if abs(k[0] - 0.85) < 0.05]
    assert relevant, "Expected at least one knot near 0.85"
    raw, cal = relevant[0]
    delta = cal - raw
    assert abs(delta) <= calib_module.MAX_DELTA + 0.001


def test_build_knots_empty_returns_identity(calib_module):
    assert calib_module.build_knots([]) == [[0.0, 0.0], [1.0, 1.0]]


def test_build_knots_monotone_output(calib_module):
    """Knots are non-decreasing in calibrated value (after PAV)."""
    # Mix of hits/misses across the probability range.
    trades = [
        (0.1, 0), (0.1, 0), (0.1, 1),
        (0.3, 0), (0.3, 1), (0.3, 1),
        (0.5, 0), (0.5, 1),
        (0.7, 1), (0.7, 1), (0.7, 0),
        (0.9, 1), (0.9, 1), (0.9, 1),
    ]
    knots = calib_module.build_knots(trades)
    cal_values = [k[1] for k in knots]
    for a, b in zip(cal_values, cal_values[1:]):
        assert a <= b + 1e-6, f"Non-monotone: {a} > {b}"


# ─── End-to-end with DB ──────────────────────────────────────────────────────


def _seed(seed_prediction, *, model_p, hit, days_ago=1):
    return seed_prediction(
        market_id=f"KXFAKE-{model_p:.4f}-{hit}",
        edge=0.20,
        proposed_side="yes",
        decision="approved",
        mode="live",
        contracts_filled=1,
        fill_price_cents=30,
        model_prob_yes=model_p,
        realized_pnl_usd=0.5 if hit else -0.5,
        kalshi_settled_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        resolved_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def test_fetch_trades_only_settled_live(
    calib_module, db_conn, mock_get_connection, seed_prediction
):
    _seed(seed_prediction, model_p=0.4, hit=1)
    _seed(seed_prediction, model_p=0.6, hit=0)
    trades = calib_module.fetch_trades()
    pairs = sorted(trades)
    assert pairs == [(0.4, 1), (0.6, 0)]


def test_main_writes_identity_below_min_samples(
    calib_module, db_conn, mock_get_connection, db_cursor, seed_prediction
):
    """With < 30 samples, the block stores the identity fallback."""
    for i in range(5):
        _seed(seed_prediction, model_p=0.3 + i * 0.1, hit=i % 2)
    rc = calib_module.main()
    assert rc == 0
    db_cursor.execute(
        "SELECT content FROM agent_memory_blocks "
        "WHERE block_name = %s AND tenant_id = %s",
        (calib_module.BLOCK_NAME, "delphi"),
    )
    import json as _json
    payload = _json.loads(db_cursor.fetchone()["content"])
    assert payload["fallback"] == "identity"
    assert payload["knots"] == [[0.0, 0.0], [1.0, 1.0]]


def test_main_writes_isotonic_above_min_samples(
    calib_module, db_conn, mock_get_connection, db_cursor, seed_prediction
):
    """With 500+ samples spanning the probability range, isotonic produces
    multiple knots. MIN_SAMPLES raised to 500 to require adequate data depth.
    """
    # Inverted: 0.9 model probabilities → 10% hit rate (overconfident YES).
    # 0.1 model probabilities → 60% hit rate.
    import random
    random.seed(0)
    # Need >= 500 samples to pass new MIN_SAMPLES guard
    for _ in range(300):
        _seed(seed_prediction, model_p=0.1 + random.random() * 0.05,
              hit=1 if random.random() < 0.6 else 0)
    for _ in range(200):
        _seed(seed_prediction, model_p=0.85 + random.random() * 0.10,
              hit=1 if random.random() < 0.10 else 0)
    rc = calib_module.main()
    assert rc == 0
    db_cursor.execute(
        "SELECT content FROM agent_memory_blocks "
        "WHERE block_name = %s AND tenant_id = %s",
        (calib_module.BLOCK_NAME, "delphi"),
    )
    import json as _json
    payload = _json.loads(db_cursor.fetchone()["content"])
    assert payload["fallback"] == "isotonic"
    assert payload["n_samples"] == 500
    knots = payload["knots"]
    assert len(knots) >= 3
    # Check endpoints pinned.
    assert knots[0] == [0.0, 0.0]
    assert knots[-1] == [1.0, 1.0]
