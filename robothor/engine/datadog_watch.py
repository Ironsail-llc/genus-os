"""Genus watches Datadog: error spikes in a tenant's services, and the link itself.

Two read-only observers, run from the daemon watchdog beside
:mod:`robothor.engine.detectors` (every 5 minutes, scheduler leader only):

* **Error spike.** For each service inside the tenant's Datadog scope, the
  count of ``status:error`` logs in the latest complete 15-minute window is
  compared with the same count over the 8 windows before it (two hours). It
  fires when the count is at least :data:`SPIKE_MIN_COUNT` and above both
  :data:`SPIKE_RATIO` times the baseline median and the median plus
  :data:`SPIKE_SPREAD` robust deviations. A service with no errors in the
  baseline fires only at :data:`NEW_SERVICE_MIN_COUNT`.
* **Connection lost.** On the root tenant's engine only, Datadog rejecting
  the key or being unreachable on :data:`CONNECTION_FAILURES_FOR_LOST`
  consecutive checks is one finding; the first good check after it is
  ``connection_restored``.

Where it runs: each engine watches its OWN tenant (``EngineConfig.tenant_id``),
so every tenant with an engine is covered exactly once and a delivered alert
lands in that tenant's own alert digest -- :func:`robothor.engine.alerts.alert`
has no tenant parameter and always writes to the process's tenant, so this is
the only placement under which ``deliver`` reaches the right people. The
engine whose tenant has no parent in ``crm_tenants`` also owns the
connection check, so a lost key is reported once, not once per engine.

Every finding is one ``datadog.watch_finding`` audit event in the tenant it
concerns, plus one ``event=datadog_watch`` log line. Under the default
``ROBOTHOR_DATADOG_WATCH_MODE=dry_run`` nothing is delivered; under
``deliver`` the same brief goes through ``alerts.alert``. Briefs carry counts
and service names only, never a log message.

These observers flag only: nothing here stops, pauses or limits anything.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from robothor.integrations.datadog.client import DatadogError
from robothor.integrations.datadog.scope import scope_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from robothor.integrations.datadog.client import AsyncDatadogClient
    from robothor.integrations.datadog.scope import DatadogScope

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_WINDOWS",
    "CONNECTION_FAILURES_FOR_LOST",
    "NEW_SERVICE_MIN_COUNT",
    "SPIKE_MIN_COUNT",
    "DatadogWatch",
    "SpikeFinding",
    "check_connection",
    "check_error_spike",
    "datadog_watch_detector",
    "reset_state",
    "watch_mode",
]

# ── detection thresholds (not usage limits) ─────────────────────────────────

#: One window: long enough that a handful of errors is not a spike, short
#: enough that a real one is seen within a quarter of an hour.
WINDOW_MINUTES = 15
#: Baseline windows before the current one: two hours of the same slot size.
BASELINE_WINDOWS = 8
#: A window is read only once it ended this long ago, so late-arriving logs
#: are mostly in before it is counted (and cached).
INGEST_LAG_MINUTES = 2
#: Below this many errors in 15 minutes nothing is worth a brief, whatever the ratio.
SPIKE_MIN_COUNT = 20
#: A service with no usual level (new, silent, or a median of zero: a stray
#: error or two in the baseline) must clear this: a startup burst is not an incident.
NEW_SERVICE_MIN_COUNT = 100
#: At least this many times the usual (median) count.
SPIKE_RATIO = 3.0
#: And at least this many robust deviations above the median, so a noisy
#: service needs a bigger jump than a steady one.
SPIKE_SPREAD = 5.0
#: Scales the median absolute deviation to a standard deviation for normal data.
MAD_SCALE = 1.4826
#: Consecutive failed checks before the link counts as lost: one blip is not an outage.
CONNECTION_FAILURES_FOR_LOST = 3
#: Error kinds that mean the link is broken (key rejected, Datadog unreachable).
_LINK_FAILURES = frozenset({"auth", "network"})
#: Groups asked for per aggregate: fits the API's group page, so every service
#: in a tenant's scope is counted. Not a limit on what is watched.
GROUP_PAGE = 1000

Mode = Literal["dry_run", "deliver"]
ConnectionVerdict = Literal["lost", "restored", "none"]


# ── the decision functions ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SpikeFinding:
    service: str
    current: int
    baseline_median: float
    baseline_spread: float
    new_service: bool


def check_error_spike(
    current: Mapping[str, int], baseline: Mapping[str, Sequence[int]]
) -> list[SpikeFinding]:
    """The services whose current error count is a spike against their own baseline.

    ``baseline[service]`` is the count in each preceding window. A service
    missing from it, or with no errors in any window, has no baseline. One
    whose median is zero has no usual level either and needs the same higher
    floor, so the rule has no jump between "silent" and "one stray error".
    """
    found: list[SpikeFinding] = []
    for service, count in sorted(current.items()):
        history = list(baseline.get(service) or ())
        if not any(history):
            if count >= NEW_SERVICE_MIN_COUNT:
                found.append(SpikeFinding(service, count, 0.0, 0.0, new_service=True))
            continue
        median = float(statistics.median(history))
        spread = MAD_SCALE * float(statistics.median(abs(x - median) for x in history))
        threshold = max(SPIKE_RATIO * median, median + SPIKE_SPREAD * spread)
        floor = NEW_SERVICE_MIN_COUNT if median == 0 else SPIKE_MIN_COUNT
        if count >= floor and count > threshold:
            found.append(SpikeFinding(service, count, median, spread, new_service=False))
    return found


def check_connection(results: Sequence[str], *, lost: bool) -> ConnectionVerdict:
    """``lost``, ``restored`` or ``none`` from recent check outcomes (oldest first).

    An outcome is ``"ok"`` or a :class:`DatadogError` kind. Only ``auth`` and
    ``network`` count as failures; anything else breaks a streak without
    restoring.
    """
    if lost:
        return "restored" if results and results[-1] == "ok" else "none"
    tail = list(results)[-CONNECTION_FAILURES_FOR_LOST:]
    if len(tail) == CONNECTION_FAILURES_FOR_LOST and all(r in _LINK_FAILURES for r in tail):
        return "lost"
    return "none"


# ── windows ─────────────────────────────────────────────────────────────────


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """The current window first, then the baseline windows, newest to oldest."""
    size = timedelta(minutes=WINDOW_MINUTES)
    settled = now.astimezone(UTC) - timedelta(minutes=INGEST_LAG_MINUTES)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    end = epoch + size * ((settled - epoch) // size)
    return [(end - size * (i + 1), end - size * i) for i in range(1 + BASELINE_WINDOWS)]


# ── the watch ───────────────────────────────────────────────────────────────


def watch_mode() -> Mode:
    from robothor.settings import get_settings

    mode = get_settings().integrations.datadog_watch_mode
    return "deliver" if mode == "deliver" else "dry_run"


async def _default_client() -> AsyncDatadogClient:
    from robothor.integrations.datadog.client import async_client_from_settings

    # The vault read blocks: off the loop.
    return await asyncio.to_thread(async_client_from_settings)


async def _default_is_root(tenant_id: str) -> bool:
    """No parent in ``crm_tenants`` (or no row: a single-engine install)."""
    from robothor.crm import dal

    row = await asyncio.to_thread(dal.get_tenant, tenant_id)
    # get_tenant returns the API shape (tenant_to_dict), not the raw row.
    return row is None or not row.get("parentTenantId")


def _would_notify(tenant_id: str) -> str:
    # alerts.alert at warning level writes an alert_digest row for the main
    # agent of the engine's tenant, which its heartbeat and briefing surface.
    return f"main agent alert digest, tenant {tenant_id}"


class DatadogWatch:
    """The watch's state for one engine: cached windows, link history, dedup."""

    def __init__(
        self,
        *,
        home_tenant: str,
        client_factory: Callable[[], Awaitable[AsyncDatadogClient]] = _default_client,
        is_root: Callable[[str], Awaitable[bool]] = _default_is_root,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        mode: Callable[[], Mode] = watch_mode,
    ) -> None:
        self.home_tenant = home_tenant
        self._client_factory = client_factory
        self._is_root = is_root
        self._clock = clock
        self._mode = mode
        self._counts: dict[str, dict[str, dict[str, int]]] = {}
        self._outcomes: deque[str] = deque(maxlen=CONNECTION_FAILURES_FOR_LOST + 1)
        self._lost = False
        self._episode = ""
        self._said: set[str] = set()

    def _once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._said:
            self._said.add(key)
            logger.info(message, *args)

    async def tick(self, tenants: Sequence[str]) -> int:
        """One pass over ``tenants``; returns the findings recorded."""
        try:
            client = await self._client_factory()
        except DatadogError as exc:
            self._once(f"client:{exc.kind}", "datadog watch: not running (%s)", exc.kind)
            return 0
        windows = _windows(self._clock())
        failures: list[str] = []
        recorded = 0
        for tenant in tenants:
            try:
                recorded += await self._watch_tenant(client, tenant, windows, failures)
            except DatadogError as exc:
                failures.append(exc.kind)
                logger.info("datadog watch: tenant %s skipped this tick (%s)", tenant, exc.kind)
            except Exception as exc:  # noqa: BLE001 - one tenant never stops the others
                logger.warning(
                    "datadog watch: tenant %s failed this tick (%s)", tenant, type(exc).__name__
                )
        recorded += await self._watch_connection(client, failures)
        return recorded

    async def _watch_tenant(
        self,
        client: AsyncDatadogClient,
        tenant: str,
        windows: list[tuple[datetime, datetime]],
        failures: list[str],
    ) -> int:
        scope = scope_for(tenant)
        if scope is None:
            self._once(
                f"scope:{tenant}", "datadog watch: tenant %s has no Datadog scope; skipped", tenant
            )
            return 0
        counts = [await self._count(client, scope, start, end) for start, end in windows]
        cache = self._counts.setdefault(tenant, {})
        keep = {_iso(start) for start, _ in windows}
        for stale in [k for k in cache if k not in keep]:
            del cache[stale]
        current, history = counts[0], counts[1:]
        seen = {service for window in history for service in window}
        baseline = {s: [w.get(s, 0) for w in history] for s in seen}
        start, end = windows[0]
        recorded = 0
        for finding in check_error_spike(current, baseline):
            recorded += await self._spike(tenant, finding, start, end)
        return recorded

    async def _count(
        self, client: AsyncDatadogClient, scope: DatadogScope, start: datetime, end: datetime
    ) -> dict[str, int]:
        cache = self._counts.setdefault(scope.tenant_id, {})
        key = _iso(start)
        if key not in cache:
            buckets = await client.aggregate_logs(
                scope,
                "status:error",
                start=_iso(start),
                end=_iso(end),
                group_by=["service"],
                limit=GROUP_PAGE,
            )
            per_service: dict[str, int] = {}
            for bucket in buckets:
                service = bucket.by.get("service") or "(no service)"
                per_service[service] = per_service.get(service, 0) + bucket.count
            cache[key] = per_service
        return cache[key]

    async def _spike(self, tenant: str, f: SpikeFinding, start: datetime, end: datetime) -> int:
        from robothor.engine.detectors import _should_fire

        fingerprint = f"datadog_spike:{tenant}:{f.service}"
        if not _should_fire(fingerprint):
            return 0
        hours = BASELINE_WINDOWS * WINDOW_MINUTES // 60
        when = f"the {WINDOW_MINUTES} minutes to {end.strftime('%H:%M')} UTC"
        if f.new_service:
            brief = (
                f"{f.service} logged {f.current} errors in {when}, with none in the "
                f"{hours} hours before. Check its recent deploys and its error logs in Datadog."
            )
        else:
            brief = (
                f"{f.service} logged {f.current} errors in {when}, against a usual "
                f"{f.baseline_median:g} per {WINDOW_MINUTES} minutes over the previous {hours} "
                f"hours. Check its recent deploys and its error logs in Datadog."
            )
        details = {
            "service": f.service,
            "current": f.current,
            "baseline": {
                "median": f.baseline_median,
                "spread": round(f.baseline_spread, 2),
                "windows": BASELINE_WINDOWS,
                "new_service": f.new_service,
            },
            "window": {"start": _iso(start), "end": _iso(end), "minutes": WINDOW_MINUTES},
        }
        title = f"Datadog error spike: {f.service}"
        # The window in the id, so a spike in a later window is never
        # suppressed by the alert cooldown of an earlier one.
        alert_id = f"{fingerprint}:{_iso(end)}"
        await self._finding(tenant, "error_spike", title, brief, details, alert_id)
        return 1

    async def _watch_connection(self, client: AsyncDatadogClient, failures: list[str]) -> int:
        try:
            if not await self._is_root(self.home_tenant):
                return 0
        except Exception as exc:  # noqa: BLE001 - decided again next tick
            logger.info("datadog watch: tenant tree unreadable (%s)", type(exc).__name__)
            return 0
        outcome = next((k for k in failures if k in _LINK_FAILURES), None)
        rejected = "a key"
        if outcome is None:
            # /api/v1/validate checks only the API key; the probe (a log search
            # that matches nothing) proves the application key too.
            checks = (
                ("the API key", client.validate),
                ("the application key", client.validate_app_key),
            )
            outcome = "ok"
            for name, check in checks:
                try:
                    await check()
                except DatadogError as exc:
                    outcome, rejected = exc.kind, name
                    break
        self._outcomes.append(outcome)
        verdict = check_connection(list(self._outcomes), lost=self._lost)
        if verdict == "none":
            return 0
        tenant = self.home_tenant
        if verdict == "lost":
            self._lost = True
            self._episode = _iso(self._clock())
            cause = (
                f"Datadog rejected {rejected}"
                if self._outcomes[-1] == "auth"
                else "Datadog could not be reached"
            )
            brief = (
                f"Genus has lost its Datadog link: {cause} on the last "
                f"{CONNECTION_FAILURES_FOR_LOST} checks. Error spikes are not being watched "
                "until it works again; run `genus doctor --only datadog.reachable`."
            )
            kind, title = "connection_lost", "Datadog link lost"
        else:
            self._lost = False
            brief = "Genus can read Datadog again. Error-spike watching has resumed."
            kind, title = "connection_restored", "Datadog link restored"
        details = {"service": None, "checks": list(self._outcomes)}
        self._outcomes.clear()
        # One alert id per episode: a delivered id is suppressed for the
        # alert cooldown, and a second loss inside it is a new episode.
        alert_id = f"datadog_link:{tenant}:{kind}:{self._episode}"
        await self._finding(tenant, kind, title, brief, details, alert_id)
        return 1

    async def _finding(
        self,
        tenant: str,
        kind: str,
        title: str,
        brief: str,
        details: dict[str, Any],
        alert_id: str,
    ) -> None:
        """Record one finding; deliver it too under ``deliver`` for this engine's tenant."""
        mode = self._mode()
        if mode == "deliver" and tenant != self.home_tenant:
            # alerts.alert writes to this engine's tenant only; never misroute.
            logger.warning(
                "datadog watch: a finding for tenant %s cannot be delivered from this engine",
                tenant,
            )
            mode = "dry_run"
        event = {
            "kind": kind,
            **details,
            "mode": mode,
            "would_notify": _would_notify(tenant),
            "brief": brief,
        }
        from robothor.audit import logger as audit
        from robothor.engine.event_writer import get_event_writer
        from robothor.engine.log_fields import kv_line

        get_event_writer().submit(
            audit.log_event,
            "datadog.watch_finding",
            kind,
            category="datadog",
            actor="engine",
            details=event,
            target=details.get("service") or None,
            tenant_id=tenant,
        )
        logger.warning(
            "%s",
            kv_line(
                "datadog_watch",
                tenant=tenant,
                kind=kind,
                service=details.get("service"),
                mode=mode,
            ),
        )
        if mode == "deliver":
            from robothor.engine import alerts

            level = "info" if kind == "connection_restored" else "warning"
            await alerts.alert(
                level,
                title,
                brief,
                alert_id=alert_id,
                kind="datadog_watch",
                metadata={"tenant_id": tenant, "kind": kind, "service": details.get("service")},
            )


# ── the daemon's entry point ────────────────────────────────────────────────

_watches: dict[str, DatadogWatch] = {}
#: The pass in flight, if any. Datadog calls take up to the client timeout
#: each, so a pass runs beside the watchdog loop, never inside it.
_pass: asyncio.Task[None] | None = None


def reset_state() -> None:
    """Forget every engine-level watch (tests)."""
    global _pass  # noqa: PLW0603
    _watches.clear()
    _pass = None


def start_datadog_watch(tenant_id: str) -> bool:
    """Start one watch pass in the background; False when one is still running.

    The watchdog calls this on its 5-minute tick and moves straight on, so a
    slow or hanging Datadog never delays its pings, the approval driver or
    schedule reconciliation. A tick that finds the last pass still running
    skips, so passes never pile up.
    """
    global _pass  # noqa: PLW0603
    if _pass is not None and not _pass.done():
        logger.info("datadog watch: the previous pass is still running; this tick skips")
        return False
    from robothor.engine.task_registry import get_task_registry

    _pass = get_task_registry().spawn(_run_pass(tenant_id), name="datadog-watch")
    return True


async def wait_for_pass() -> None:
    """Return once the pass in flight (if any) has finished."""
    if _pass is not None:
        await asyncio.wait({_pass})


async def _run_pass(tenant_id: str) -> None:
    try:
        recorded = await datadog_watch_detector(tenant_id)
    except Exception as exc:  # noqa: BLE001 - the alarm's own failure is a WARNING; next tick retries
        logger.warning("datadog watch: pass failed: %s", exc)
        return
    if recorded:
        logger.info("datadog watch: %d finding(s) recorded", recorded)


def _scope_map_declared() -> bool:
    from robothor.settings import get_settings

    return bool((get_settings().integrations.datadog_scopes or "").strip())


async def datadog_watch_detector(tenant_id: str) -> int:
    """One watch pass for this engine's tenant; returns the findings recorded.

    Does nothing when the detectors are off, on a follower replica, or when no
    scope map is declared (the instance does not read Datadog).
    """
    from robothor.engine.detectors import detectors_enabled
    from robothor.engine.leader import is_leader

    if not detectors_enabled() or not is_leader() or not _scope_map_declared():
        return 0
    watch = _watches.get(tenant_id)
    if watch is None:
        watch = _watches[tenant_id] = DatadogWatch(
            home_tenant=tenant_id, client_factory=_client_factory_ref()
        )
    return await watch.tick([tenant_id])


def _client_factory_ref() -> Callable[[], Awaitable[AsyncDatadogClient]]:
    # Looked up at call time so a test can replace ``_default_client``.
    async def factory() -> AsyncDatadogClient:
        return await _default_client()

    return factory
