"""Phase 4 — apply daily-tuner outputs (bias correction + calibration
shrinkage) to live predictions.

Two memory blocks feed this module:

  - ``delphi-bias-correction``       — per-(city, source, lead_band) bias in °F
  - ``delphi-calibration-shrinkage`` — piecewise-linear knot list mapping
                                         raw model probability → calibrated

Both are produced by daily tuner crons and consumed by the weather method
(`predict`) and `delphi_predict.py` respectively. The block reads are cached
for the duration of one predict run, since memory blocks change at most once
per day; we don't want N database hits for N markets.

Env gates allow safe rollout:
  - ``DELPHI_APPLY_BIAS=1``        — turn on bias correction
  - ``DELPHI_APPLY_CALIBRATION=1`` — turn on calibration shrinkage

When unset (default during initial Phase 4B rollout), the helpers return
inputs unchanged. Once we trust the data, flip both to "1".
"""
from __future__ import annotations

import json
import logging
import os
from typing import Iterable

from .db import DELPHI_TENANT_ID, get_connection

logger = logging.getLogger(__name__)

BIAS_BLOCK = "delphi-bias-correction"
CALIB_BLOCK = "delphi-calibration-shrinkage"


def _read_block(name: str) -> dict | None:
    sql = """
        SELECT content FROM agent_memory_blocks
        WHERE block_name = %s AND tenant_id = %s
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (name, DELPHI_TENANT_ID))
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return dict(json.loads(row[0]))
    except (json.JSONDecodeError, TypeError):
        return None


# ─── Bias correction ─────────────────────────────────────────────────────────


def _bias_enabled() -> bool:
    return os.environ.get("DELPHI_APPLY_BIAS", "") == "1"


def _lead_band(lead_hours: int | float | None) -> str | None:
    if lead_hours is None:
        return None
    h = float(lead_hours)
    if h < 0:
        return None
    if h <= 12:
        return "0-12"
    if h <= 24:
        return "12-24"
    if h <= 48:
        return "24-48"
    if h <= 72:
        return "48-72"
    return None


_BIAS_CACHE: dict | None = None


def load_bias_map() -> dict[str, float]:
    """Return the {key: bias_degF} map. Empty dict if block missing or feature off."""
    global _BIAS_CACHE
    if not _bias_enabled():
        return {}
    if _BIAS_CACHE is None:
        block = _read_block(BIAS_BLOCK) or {}
        _BIAS_CACHE = block.get("biases") or {}
    return _BIAS_CACHE


def apply_bias_to_members(
    members: Iterable[float],
    *,
    city: str,
    source: str,
    lead_hours: int | float,
) -> list[float]:
    """Add per-(city, source, lead_band) bias to each ensemble member.

    Bias is stored as mean(actual − forecast). A positive bias means the model
    has been forecasting too LOW (actual > forecast on average) — adding the
    bias shifts the forecast UP to match historical actuals. A negative bias
    means model runs hot — adding a negative value shifts DOWN.

    Formula: corrected = raw + bias   (since bias = actual - forecast)

    No-op when feature flag is off or no bias entry exists for the key.
    """
    members_list = [float(m) for m in members]
    if not _bias_enabled():
        return members_list
    band = _lead_band(lead_hours)
    if not band:
        return members_list
    biases = load_bias_map()
    key = f"{city}|{source}|{band}"
    bias = biases.get(key, 0.0)
    if bias == 0.0:
        return members_list
    return [m + bias for m in members_list]  # bias = actual - forecast; add to correct


# ─── Calibration shrinkage ───────────────────────────────────────────────────


def _calibration_enabled() -> bool:
    return os.environ.get("DELPHI_APPLY_CALIBRATION", "") == "1"


_CALIB_CACHE: dict | None = None


def load_calibration_map() -> dict:
    """Return the calibration block as a dict. Empty when off or no block."""
    global _CALIB_CACHE
    if not _calibration_enabled():
        return {}
    if _CALIB_CACHE is None:
        _CALIB_CACHE = _read_block(CALIB_BLOCK) or {}
    return _CALIB_CACHE


def _interp(x: float, knots: list[list[float]]) -> float:
    """Piecewise-linear interpolation of x against (sorted) knots [(x_i, y_i)].

    Out-of-range x clamped to nearest knot's y.
    """
    if not knots:
        return x
    if x <= knots[0][0]:
        return knots[0][1]
    if x >= knots[-1][0]:
        return knots[-1][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return x


def apply_calibration(prob: float) -> float:
    """Map a raw model probability through the isotonic calibration knots.

    No-op when feature flag is off or block is missing/identity. Output
    clamped to [0, 1] to defend against bad lookup data.
    """
    if not _calibration_enabled():
        return prob
    calib = load_calibration_map()
    if not calib or calib.get("fallback") == "identity":
        return prob
    knots = calib.get("knots") or []
    out = _interp(float(prob), knots)
    return max(0.0, min(1.0, out))


def reset_caches() -> None:
    """Test hook — clear cached blocks so next call re-reads from DB."""
    global _BIAS_CACHE, _CALIB_CACHE
    _BIAS_CACHE = None
    _CALIB_CACHE = None
