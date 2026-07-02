"""Tests for brain.delphi.corrections — bias correction + calibration
shrinkage helpers used by predict.py and the weather method.
"""
from __future__ import annotations

import json

import pytest

from brain.delphi import corrections as corr


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_caches_and_env(monkeypatch):
    """Clear module-level caches between tests."""
    corr.reset_caches()
    monkeypatch.delenv("DELPHI_APPLY_BIAS", raising=False)
    monkeypatch.delenv("DELPHI_APPLY_CALIBRATION", raising=False)
    yield
    corr.reset_caches()


def _seed_block(db_cursor, name, payload):
    db_cursor.execute(
        "DELETE FROM agent_memory_blocks WHERE block_name = %s AND tenant_id = %s",
        (name, "delphi"),
    )
    db_cursor.execute(
        """INSERT INTO agent_memory_blocks
             (block_name, block_type, content, max_chars, tenant_id)
           VALUES (%s, 'system', %s, 32000, %s)""",
        (name, json.dumps(payload), "delphi"),
    )


# ─── Lead band ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "h,expected",
    [
        (6, "0-12"),
        (12, "0-12"),
        (24, "12-24"),
        (48, "24-48"),
        (72, "48-72"),
        (73, None),
        (None, None),
    ],
)
def test_lead_band(h, expected):
    assert corr._lead_band(h) == expected


# ─── Bias correction ─────────────────────────────────────────────────────────


def test_bias_disabled_by_default():
    members = [70.0, 71.0, 72.0]
    out = corr.apply_bias_to_members(
        members, city="NYC", source="GFS", lead_hours=24,
    )
    assert out == [70.0, 71.0, 72.0]


def test_bias_no_op_when_block_missing(monkeypatch):
    monkeypatch.setenv("DELPHI_APPLY_BIAS", "1")
    corr.reset_caches()  # clear any stale cache from other tests / production DB
    # With no DB mock, load_bias_map returns {} (no block found) → no-op
    # This test requires the test DB to have no bias block for NYC|GFS|12-24.
    # Since we can't guarantee isolation from prod DB in this test env,
    # we mock _read_block to return None.
    import unittest.mock as _mock
    with _mock.patch.object(corr, "_read_block", return_value=None):
        out = corr.apply_bias_to_members(
            [70.0, 71.0], city="NYC", source="GFS", lead_hours=24,
        )
    assert out == [70.0, 71.0]
    corr.reset_caches()  # clean up


def test_bias_applies_when_enabled(
    monkeypatch, db_conn, mock_get_connection, db_cursor,
):
    monkeypatch.setenv("DELPHI_APPLY_BIAS", "1")
    _seed_block(db_cursor, corr.BIAS_BLOCK, {"biases": {"NYC|GFS|12-24": 1.5}})
    out = corr.apply_bias_to_members(
        [70.0, 71.0], city="NYC", source="GFS", lead_hours=24,
    )
    assert out == pytest.approx([71.5, 72.5])  # bias=+1.5: m + bias (actual > forecast by 1.5)


def test_bias_unknown_key_no_op(
    monkeypatch, db_conn, mock_get_connection, db_cursor,
):
    monkeypatch.setenv("DELPHI_APPLY_BIAS", "1")
    _seed_block(db_cursor, corr.BIAS_BLOCK, {"biases": {"OTHER|GFS|12-24": 1.5}})
    out = corr.apply_bias_to_members(
        [70.0, 71.0], city="NYC", source="GFS", lead_hours=24,
    )
    assert out == [70.0, 71.0]


def test_bias_caches_block_read(
    monkeypatch, db_conn, mock_get_connection, db_cursor,
):
    """Cache: subsequent calls don't re-hit the DB."""
    monkeypatch.setenv("DELPHI_APPLY_BIAS", "1")
    _seed_block(db_cursor, corr.BIAS_BLOCK, {"biases": {"NYC|GFS|0-12": 0.5}})
    # First call populates cache.
    corr.apply_bias_to_members([70], city="NYC", source="GFS", lead_hours=6)
    # Mutate block — should not affect cached result.
    _seed_block(db_cursor, corr.BIAS_BLOCK, {"biases": {"NYC|GFS|0-12": 99.0}})
    out = corr.apply_bias_to_members([70], city="NYC", source="GFS", lead_hours=6)
    # Still uses cached 0.5.
    assert out == pytest.approx([70.5])  # bias=+0.5: m + bias


# ─── Interpolation ──────────────────────────────────────────────────────────


def test_interp_endpoints():
    knots = [[0.0, 0.0], [0.5, 0.4], [1.0, 1.0]]
    assert corr._interp(0.0, knots) == 0.0
    assert corr._interp(1.0, knots) == 1.0


def test_interp_midpoint_linear():
    knots = [[0.0, 0.0], [0.5, 0.4], [1.0, 1.0]]
    assert corr._interp(0.25, knots) == pytest.approx(0.20)


def test_interp_extrapolation_clamped():
    knots = [[0.10, 0.20], [0.90, 0.80]]
    assert corr._interp(-0.5, knots) == 0.20
    assert corr._interp(2.0, knots) == 0.80


def test_interp_empty_returns_input():
    assert corr._interp(0.5, []) == 0.5


# ─── Calibration shrinkage ──────────────────────────────────────────────────


def test_calibration_disabled_by_default():
    assert corr.apply_calibration(0.85) == 0.85


def test_calibration_identity_block_no_op(
    monkeypatch, db_conn, mock_get_connection, db_cursor,
):
    monkeypatch.setenv("DELPHI_APPLY_CALIBRATION", "1")
    _seed_block(db_cursor, corr.CALIB_BLOCK, {
        "fallback": "identity",
        "knots": [[0.0, 0.0], [1.0, 1.0]],
    })
    assert corr.apply_calibration(0.85) == 0.85


def test_calibration_applies_isotonic_block(
    monkeypatch, db_conn, mock_get_connection, db_cursor,
):
    monkeypatch.setenv("DELPHI_APPLY_CALIBRATION", "1")
    # Overconfident: model says 0.85 → realized 0.30.
    _seed_block(db_cursor, corr.CALIB_BLOCK, {
        "fallback": "isotonic",
        "knots": [[0.0, 0.0], [0.50, 0.40], [0.85, 0.30], [1.0, 1.0]],
    })
    out = corr.apply_calibration(0.85)
    assert out == pytest.approx(0.30, abs=0.01)


def test_calibration_clamps_to_unit_interval(
    monkeypatch, db_conn, mock_get_connection, db_cursor,
):
    monkeypatch.setenv("DELPHI_APPLY_CALIBRATION", "1")
    _seed_block(db_cursor, corr.CALIB_BLOCK, {
        "fallback": "isotonic",
        "knots": [[0.0, -0.2], [1.0, 1.5]],
    })
    assert corr.apply_calibration(0.0) == 0.0
    assert corr.apply_calibration(1.0) == 1.0
