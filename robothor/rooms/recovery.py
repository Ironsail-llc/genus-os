"""Failing ``building`` editions whose run is gone.

A ``building`` edition holds its period: while it exists, asking for that
period returns it instead of starting a run. A run that is cancelled, or an
engine that stops mid-run, would otherwise leave the period held for good.

Not alive, from what this engine can know:

- a run of THIS process that is still going (``live``) is alive, whatever its age;
- else, when ``run_id`` names an ``agent_runs`` row, that row decides: pending,
  running or awaiting approval is alive, any ended state is not;
- else (no run row): not alive once the engine has started since the edition
  was created (``booted_at``; the daemon's ``ROBOTHOR_DAEMON_START_TS`` when not
  given), or once it has been building for ``STALE_BUILDING_SECONDS`` (2 hours).

A dead edition is failed with the reason, which frees its period. Called at
engine start (``reap_stale_editions(tenant_id, booted_at=boot)``) and lazily for
one room before a period is looked up.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from robothor.rooms import dal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from robothor.rooms.models import Edition

__all__ = ["STALE_BUILDING_SECONDS", "hold", "live", "live_edition_ids", "reap_stale_editions"]

logger = logging.getLogger(__name__)

#: How long an edition with no run row may stay ``building``.
STALE_BUILDING_SECONDS = 2 * 60 * 60

_ACTIVE_RUN_STATES = frozenset({"pending", "running", "awaiting_approval"})

_guard = threading.Lock()
_live: dict[str, int] = {}


def hold(edition_id: str) -> Callable[[], None]:
    """Mark ``edition_id``'s run as going in this process; call the result once
    when it ends. Taken when the run is scheduled, before it first runs."""
    with _guard:
        _live[edition_id] = _live.get(edition_id, 0) + 1
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        with _guard:
            count = _live.get(edition_id, 0) - 1
            if count > 0:
                _live[edition_id] = count
            else:
                _live.pop(edition_id, None)

    return release


@contextlib.contextmanager
def live(edition_id: str) -> Iterator[None]:
    """``hold`` for the duration of a block."""
    release = hold(edition_id)
    try:
        yield
    finally:
        release()


def live_edition_ids() -> frozenset[str]:
    """Editions whose run is going in this process now."""
    with _guard:
        return frozenset(_live)


def _boot() -> datetime | None:
    from robothor.settings import get_settings

    raw = get_settings().engine.daemon_start_ts
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _why_dead(
    edition: Edition,
    run_status: str | None,
    *,
    booted_at: datetime | None,
    now: datetime,
    live_ids: frozenset[str],
) -> str | None:
    if edition.id in live_ids:
        return None
    if run_status is not None:
        if run_status in _ACTIVE_RUN_STATES:
            return None
        return f"the run ended ({run_status}) without publishing this edition"
    created = edition.created_at or now
    if booted_at is not None and created < booted_at:
        return "the engine restarted while this edition was building"
    if now - created > timedelta(seconds=STALE_BUILDING_SECONDS):
        return (
            f"no run has been building this edition for over {STALE_BUILDING_SECONDS // 60} minutes"
        )
    return None


def reap_stale_editions(
    tenant_id: str,
    *,
    room_id: str | None = None,
    booted_at: datetime | None = None,
    now: datetime | None = None,
) -> list[Edition]:
    """Fail every ``building`` edition of the tenant (or of ``room_id``) whose run
    is not alive; the editions failed. Sync: async callers use ``asyncio.to_thread``."""
    booted_at = booted_at or _boot()
    now = now or datetime.now(UTC)
    live_ids = live_edition_ids()
    reaped: list[Edition] = []
    for edition, run_status in dal.list_building_editions(tenant_id, room_id=room_id):
        reason = _why_dead(edition, run_status, booted_at=booted_at, now=now, live_ids=live_ids)
        if reason is None:
            continue
        failed = dal.fail_edition(tenant_id, edition.id, reason)
        if failed is not None:
            logger.info("room edition %s failed: %s", edition.id, reason)
            reaped.append(failed)
    return reaped
