"""The daemon purges expired ``plugin_state`` rows on its own.

An expired row is already invisible to ``PluginState`` reads, so the purge is
about storage, not correctness — but a plugin caching with a short TTL writes
rows nobody else would ever delete. The watchdog runs it hourly, and like every
other watchdog sweep it must never take the loop down.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from robothor.engine import daemon


def test_the_sweep_purges_for_the_tenant():
    with patch("robothor.plugins.state.purge_expired", return_value=3) as purge:
        assert asyncio.run(daemon._purge_expired_plugin_state("test-tenant")) == 3
    purge.assert_called_once_with("test-tenant")


def test_a_failing_purge_never_raises():
    with patch("robothor.plugins.state.purge_expired", side_effect=ConnectionError("down")):
        assert asyncio.run(daemon._purge_expired_plugin_state("test-tenant")) == 0


def test_the_watchdog_tick_calls_it():
    """Grepped from source, as the stale-question sweep's call site is: the
    tick is a long loop that needs a scheduler and a runner to enter."""
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    assert "await _purge_expired_plugin_state(config.tenant_id)" in src
