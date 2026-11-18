"""An optional off-cycle agent with no config is skipped quietly.

The daemon's reactive curiosity loop spawned ``curiosity-engine`` off-cycle on
any instance, configured or not; where the agent has no manifest every attempt
logged "Agent config not found for cron job" at ERROR. An instance that does
not run that agent now gets one INFO line per process start and no spawn; an
instance that does is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import daemon


def _sleeps(passes: int):
    calls = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > passes + 1:  # the post-boot sleep, then one per pass
            raise asyncio.CancelledError

    return _sleep


async def _run_loop(config: Any, passes: int = 3) -> MagicMock:
    scheduler = MagicMock()
    scheduler._run_agent = AsyncMock()
    metrics = {
        "should_spawn": True,
        "orphan_count": 1,
        "thin_cluster_count": 1,
        "uncertainty_count": 1,
        "low_confidence_count": 1,
    }
    with (
        patch("robothor.engine.daemon.asyncio.sleep", side_effect=_sleeps(passes)),
        patch("robothor.engine.dedup.running_agents", return_value=set()),
        patch(
            "robothor.memory.gap_analysis.get_memory_density_metrics",
            new=AsyncMock(return_value=metrics),
        ),
        patch("robothor.engine.config.load_agent_config_or_broken", return_value=config),
    ):
        await daemon._curiosity_density_loop(scheduler)
    return scheduler


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(daemon, "_curiosity_last_spawn_ts", 0.0)
    monkeypatch.setattr(daemon, "_CURIOSITY_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(daemon, "_unconfigured_offcycle_noted", set(), raising=False)


@pytest.mark.asyncio
async def test_an_unconfigured_agent_is_skipped_with_one_info_line(caplog) -> None:
    with caplog.at_level(logging.INFO):
        scheduler = await _run_loop(None, passes=3)
    scheduler._run_agent.assert_not_awaited()
    noted = [r for r in caplog.records if "curiosity-engine" in r.getMessage()]
    assert len(noted) == 1
    assert noted[0].levelno == logging.INFO
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_a_configured_agent_is_still_spawned() -> None:
    scheduler = await _run_loop(MagicMock(), passes=1)
    scheduler._run_agent.assert_awaited_once_with("curiosity-engine")
