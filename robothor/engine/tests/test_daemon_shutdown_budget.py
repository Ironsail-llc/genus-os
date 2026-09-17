"""The engine's SIGTERM-to-exit path must fit inside the unit's TimeoutStopSec.

robothor-engine.service gives the daemon ``TimeoutStopSec`` seconds after
SIGTERM; past that systemd SIGKILLs the cgroup and files ``Failed with result
'timeout'``, which ``OnFailure=`` pages. The stop path awaits, in series, the
shutdown announcement to the operator's chat, the task-registry drain and the
Telegram dispatcher stop — and until 2026-09-17 the announcement was unbounded
and drain (10 s) + dispatcher stop (5 s) already equalled the whole 15 s with
nothing left for the steps that are not individually bounded.

Every one of those bounds now derives from ONE constant in
``robothor.engine.shutdown_budget``, and this test reads the unit file so the
constant cannot drift away from it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from robothor.engine import daemon, shutdown_budget

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE_UNIT = REPO_ROOT / "infra" / "systemd" / "robothor-engine.service"


def timeout_stop_sec() -> float:
    match = re.search(r"^TimeoutStopSec=(\d+)", ENGINE_UNIT.read_text(), re.MULTILINE)
    assert match, "robothor-engine.service has no TimeoutStopSec= — the budget has no ceiling"
    return float(match.group(1))


def test_the_budget_plus_margin_fits_inside_timeout_stop_sec():
    ceiling = timeout_stop_sec()
    needed = shutdown_budget.STOP_BUDGET_SECONDS + shutdown_budget.STOP_MARGIN_SECONDS
    assert needed <= ceiling, (
        f"budget {shutdown_budget.STOP_BUDGET_SECONDS}s + margin "
        f"{shutdown_budget.STOP_MARGIN_SECONDS}s exceeds TimeoutStopSec={ceiling:g}: "
        "systemd would SIGKILL a clean stop and page"
    )
    assert shutdown_budget.STOP_MARGIN_SECONDS > 0, (
        "the unbounded steps (scheduler, hooks, Slack, elector, NATS) need some headroom"
    )


def test_the_bounded_steps_sum_to_the_budget():
    """The three awaited bounds are slices of the one budget, not three
    numbers that happen to add up today."""
    assert (
        shutdown_budget.ANNOUNCE_TIMEOUT_SECONDS
        + shutdown_budget.DRAIN_TIMEOUT_SECONDS
        + shutdown_budget.POLLING_STOP_TIMEOUT_SECONDS
        == shutdown_budget.STOP_BUDGET_SECONDS
    )
    assert shutdown_budget.DRAIN_TIMEOUT_SECONDS > 0


def test_main_uses_the_derived_bounds_not_literals():
    """Source-level: the drain and the dispatcher stop take their timeout
    from the budget module. A literal here is a second source of truth."""
    src = daemon.__file__
    text = Path(src).read_text()
    assert "drain(timeout=DRAIN_TIMEOUT_SECONDS)" in text
    assert "polling_stop_timeout=POLLING_STOP_TIMEOUT_SECONDS" in text
    telegram_src = (Path(src).parent / "telegram.py").read_text()
    assert "wait_for(self.dp.stop_polling(), timeout=5)" not in telegram_src, (
        "telegram.stop() still carries its own literal timeout"
    )


@pytest.mark.asyncio
async def test_announce_shutdown_is_bounded(monkeypatch):
    """A Telegram send that never returns must not eat the stop budget."""
    monkeypatch.setattr(shutdown_budget, "ANNOUNCE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(daemon, "ANNOUNCE_TIMEOUT_SECONDS", 0.05)

    async def hangs(*_args, **_kwargs):
        await asyncio.sleep(3600)

    import robothor.engine.delivery as delivery

    monkeypatch.setattr(delivery, "get_telegram_sender", lambda: hangs)
    config = SimpleNamespace(default_chat_id="12345")

    await asyncio.wait_for(daemon._announce_shutdown(config), timeout=2)
