"""
Tests for agent status endpoint — health tier computation and cron monitoring.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Health Tier Computation ──────────────────────────────────────────


def test_healthy_agent():
    """Agent within schedule interval is healthy."""
    from routers.agents import _compute_health_tier

    # Last run 5 min ago, interval 10 min
    tier = _compute_health_tier(time.time() - 300, 600, 0, True, 10)
    assert tier == "healthy"


def test_degraded_agent_late():
    """Agent past 1.5x interval is degraded."""
    from routers.agents import _compute_health_tier

    # Last run 16 min ago, interval 10 min (1.5x = 15 min)
    tier = _compute_health_tier(time.time() - 960, 600, 0, True, 10)
    assert tier == "degraded"


def test_degraded_agent_one_error():
    """Agent with 1 consecutive error is degraded."""
    from routers.agents import _compute_health_tier

    tier = _compute_health_tier(time.time() - 300, 600, 1, True, 10)
    assert tier == "degraded"


def test_failed_agent_two_errors():
    """Agent with 2+ consecutive errors is failed."""
    from routers.agents import _compute_health_tier

    tier = _compute_health_tier(time.time() - 300, 600, 2, True, 10)
    assert tier == "failed"


def test_failed_agent_very_late():
    """Agent past 2x interval is failed."""
    from routers.agents import _compute_health_tier

    # Last run 25 min ago, interval 10 min (2x = 20 min)
    tier = _compute_health_tier(time.time() - 1500, 600, 0, True, 10)
    assert tier == "failed"


def test_unknown_disabled():
    """Disabled agent is unknown."""
    from routers.agents import _compute_health_tier

    tier = _compute_health_tier(time.time() - 100, 600, 0, False, 10)
    assert tier == "unknown"


def test_unknown_insufficient_runs():
    """Agent with <3 runs is unknown."""
    from routers.agents import _compute_health_tier

    tier = _compute_health_tier(time.time() - 100, 600, 0, True, 2)
    assert tier == "unknown"


# ─── Interval Parsing ─────────────────────────────────────────────────


def test_parse_interval_every_10():
    """*/10 * * * * → 600s."""
    from routers.agents import _parse_interval_seconds

    assert _parse_interval_seconds("*/10 * * * *") == 600


def test_parse_interval_every_17():
    """*/17 * * * * → 1020s."""
    from routers.agents import _parse_interval_seconds

    assert _parse_interval_seconds("*/17 * * * *") == 1020


def test_parse_interval_hourly():
    """0 * * * * → 3600s."""
    from routers.agents import _parse_interval_seconds

    assert _parse_interval_seconds("0 * * * *") == 3600


def test_parse_interval_twice_daily():
    """0 10,18 * * * → 8 hour gap."""
    from routers.agents import _parse_interval_seconds

    result = _parse_interval_seconds("0 10,18 * * *")
    assert result == 8 * 3600


def test_parse_interval_daily():
    """30 6 * * * → 86400s (daily)."""
    from routers.agents import _parse_interval_seconds

    assert _parse_interval_seconds("30 6 * * *") == 86400


def test_parse_interval_range_step():
    """0 6-22/2 * * * → 7200s (every 2h)."""
    from routers.agents import _parse_interval_seconds

    assert _parse_interval_seconds("0 6-22/2 * * *") == 7200


def test_parse_interval_range_hourly():
    """0 6-22 * * * → 3600s (hourly within range)."""
    from routers.agents import _parse_interval_seconds

    assert _parse_interval_seconds("0 6-22 * * *") == 3600


# ─── API Endpoint ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_status_endpoint(test_client):
    """GET /api/agents/status returns agent list and summary."""
    import routers.agents as agents_module

    # Clear cache
    agents_module._cache = {"data": None, "expires": 0.0}

    mock_jobs = [
        {
            "name": "email-classifier",
            "schedule": "0 6-22 * * *",
            "enabled": True,
            "lastRunAt": "2026-02-23T12:00:00+00:00",
            "consecutiveErrors": 0,
            "runCount": 50,
        },
    ]

    with patch.object(Path, "exists", return_value=True):
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: json.dumps(mock_jobs)

            # Use a simpler approach: mock _build_agent_status
            mock_result = {
                "agents": [
                    {"name": "email-classifier", "status": "healthy", "schedule": "0 6-22 * * *"}
                ],
                "summary": {"healthy": 1, "degraded": 0, "failed": 0, "unknown": 0, "total": 1},
            }
            with patch.object(agents_module, "_build_agent_status", return_value=mock_result):
                r = await test_client.get("/api/agents/status")

    assert r.status_code == 200
    data = r.json()
    assert "agents" in data
    assert "summary" in data


@pytest.mark.asyncio
async def test_agent_status_cache(test_client):
    """GET /api/agents/status uses cache within TTL."""
    import routers.agents as agents_module

    cached = {
        "agents": [{"name": "cached-agent", "status": "healthy"}],
        "summary": {"healthy": 1, "degraded": 0, "failed": 0, "unknown": 0, "total": 1},
    }
    agents_module._cache = {"data": cached, "expires": time.time() + 60}

    r = await test_client.get("/api/agents/status")
    assert r.status_code == 200
    data = r.json()
    assert data["agents"][0]["name"] == "cached-agent"

    # Cleanup
    agents_module._cache = {"data": None, "expires": 0.0}
