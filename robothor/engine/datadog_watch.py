"""Genus watches Datadog: a tenant's services and monitors, and the link itself.

Four read-only observers, run from the daemon watchdog beside
:mod:`robothor.engine.detectors` (every 5 minutes, scheduler leader only):

* **Error spike.** For each service inside the tenant's Datadog scope, the
  count of ``status:error`` logs in the latest complete 15-minute window is
  compared with the same count over the 8 windows before it (two hours). It
  fires when the count is at least :data:`SPIKE_MIN_COUNT` and above both
  :data:`SPIKE_RATIO` times the baseline median and the median plus
  :data:`SPIKE_SPREAD` robust deviations. A service whose baseline median is
  zero (new, silent, or a stray error) fires only at :data:`NEW_SERVICE_MIN_COUNT`.
* **Silent service.** The same slots, counting every log line per service: a
  service that logged in each of the 8 baseline slots, with a median of at
  least :data:`SILENT_MIN_MEDIAN` lines, and logged nothing in the current one
  is ``service_silent``; its next line is ``service_resumed``.
* **Firing monitors.** The tenant's monitors in Alert, Warn or No Data and
  not muted, read every pass. A monitor newly firing, or firing at a new status, is
  ``monitor_firing``; one recorded that is no longer firing is
  ``monitor_recovered``. A tenant with no monitor access skips this observer.
* **Connection lost.** On the root tenant's engine only, Datadog rejecting
  the API key or the application key, or being unreachable, on :data:`CONNECTION_FAILURES_FOR_LOST`
  consecutive checks is one finding; the first good check after it is
  ``connection_restored``.

Where it runs: each engine watches its OWN tenant (``EngineConfig.tenant_id``),
so every tenant with an engine is covered once, and the tenant is the one
whose alert digest the normal alerts path writes to (``alerts.alert`` has
no tenant parameter and always writes to the process's tenant). The engine whose tenant has no parent in ``crm_tenants`` also owns
the connection check, so a lost key is reported once, not once per engine.
Each pass runs as a background task, so a slow Datadog never holds the
watchdog loop.

Every finding is delivered once through the alerts path
(:func:`robothor.engine.datadog_delivery.send_alert`) at the level in
:func:`finding_level`, never ``critical``, so it reaches the main agent's
alert digest and pages no one. One alert per EPISODE: a spike that continues,
a monitor that stays firing, is not sent again until the episode closes (the
recovery, or a pass where the spike is gone); a higher level inside an
episode (Warn to Alert) is sent. Repeats of an alert id inside the alerts
cooldown are not sent either. Then the finding is one ``datadog.watch_finding``
audit event in the tenant it concerns, plus one ``event=datadog_watch`` log
line, with ``mode`` ``delivered``, ``suppressed`` or ``delivery_failed`` and
the ``delivery`` it made. A failed send never loses the finding and never
stops the pass. ``brief`` is the text sent. Briefs carry counts, service
names and masked monitor names only, never a log message.

Each observer runs on its own for each tenant: one failing never stops
another, nor another tenant. Their state (cached slots, which monitors and
services are recorded) lives in the engine process; see the docs for what a
restart means.

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
from robothor.integrations.datadog.scope import ScopeError, scope_for
from robothor.secrets.redaction import redact_personal_data

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence

    from robothor.integrations.datadog.client import AsyncDatadogClient
    from robothor.integrations.datadog.scope import DatadogScope

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_WINDOWS",
    "CONNECTION_FAILURES_FOR_LOST",
    "MONITOR_PAGE",
    "NEW_SERVICE_MIN_COUNT",
    "SILENT_MIN_MEDIAN",
    "SPIKE_MIN_COUNT",
    "DatadogWatch",
    "MonitorChange",
    "SilenceChange",
    "SpikeFinding",
    "check_connection",
    "check_error_spike",
    "check_monitors",
    "check_silence",
    "datadog_watch_detector",
    "finding_level",
    "reset_state",
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
#: One line a minute: a service usually logging less than that per slot is
#: quiet by nature, so a slot with nothing in it is not news.
SILENT_MIN_MEDIAN = WINDOW_MINUTES
#: Monitors asked for per search page; every page is read. Not a limit.
MONITOR_PAGE = 100
#: The monitors that count as firing, as the search filter spells them. A
#: muted monitor (by hand or by a downtime) is left out: Datadog holds back
#: its own notification for it, so it is not news either.
FIRING_QUERY = '(status:alert OR status:warn OR status:"no data") AND muted:false'
#: Each firing status as the search returns it (lowercased) and as a brief shows it.
_FIRING = {"alert": "Alert", "warn": "Warn", "no data": "No Data"}
#: Logs that carry no service tag are counted under this name.
_NO_SERVICE = "(no service)"

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


@dataclass(frozen=True)
class MonitorChange:
    monitor_id: str
    kind: Literal["firing", "recovered"]
    status: str | None
    previous: str | None


def check_monitors(firing: Mapping[str, str], recorded: Mapping[str, str]) -> list[MonitorChange]:
    """What changed between the monitors firing now and the ones recorded.

    Both map a monitor id to its lowercased status. A monitor firing that is
    not recorded, or recorded at another status, is ``firing``; one recorded
    that no longer fires is ``recovered``.
    """
    changes = [
        MonitorChange(monitor_id, "firing", status, recorded.get(monitor_id))
        for monitor_id, status in sorted(firing.items())
        if recorded.get(monitor_id) != status
    ]
    changes += [
        MonitorChange(monitor_id, "recovered", None, status)
        for monitor_id, status in sorted(recorded.items())
        if monitor_id not in firing
    ]
    return changes


@dataclass(frozen=True)
class SilenceChange:
    service: str
    kind: Literal["silent", "resumed"]
    current: int
    baseline_median: float


def check_silence(
    current: Mapping[str, int], baseline: Mapping[str, Sequence[int]], silent: Collection[str]
) -> list[SilenceChange]:
    """Services gone silent in the current slot, and silent ones logging again.

    Silent: no lines now, a line in every one of the :data:`BASELINE_WINDOWS`
    slots before, and a baseline median of at least :data:`SILENT_MIN_MEDIAN`.
    A service with a shorter history (new) is never silent. ``silent`` holds
    the ones already recorded: they are not found again, and resume on their
    first line.
    """
    changes: list[SilenceChange] = []
    for service, history in sorted(baseline.items()):
        if service in silent or service == _NO_SERVICE or current.get(service, 0):
            continue
        counts = list(history)
        if len(counts) < BASELINE_WINDOWS or not all(counts):
            continue
        median = float(statistics.median(counts))
        if median >= SILENT_MIN_MEDIAN:
            changes.append(SilenceChange(service, "silent", 0, median))
    changes += [
        SilenceChange(service, "resumed", current[service], 0.0)
        for service in sorted(silent)
        if current.get(service, 0)
    ]
    return changes


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


def _since(triggered: int | None, now: datetime) -> str:
    """`` for about 2 hours`` from a monitor's last trigger time, or ``""``."""
    if not triggered:
        return ""
    # The search returns seconds; tolerate milliseconds.
    seconds = triggered / 1000 if triggered > 10**11 else triggered
    minutes = int((now - datetime.fromtimestamp(seconds, UTC)).total_seconds() // 60)
    if minutes < 0:
        return ""
    if minutes < 90:
        return f" for about {max(minutes, 1)} minutes"
    if minutes < 48 * 60:
        return f" for about {round(minutes / 60)} hours"
    return f" for about {round(minutes / 1440)} days"


#: Findings that close an episode: each is sent at info.
_CLOSING = frozenset({"monitor_recovered", "service_resumed", "connection_restored"})
_RANK = {"info": 0, "warning": 1}
#: How a finding is titled in the alert digest.
_TITLES = {
    "error_spike": "Datadog error spike",
    "service_silent": "Datadog service silent",
    "service_resumed": "Datadog service logging again",
    "monitor_firing": "Datadog monitor firing",
    "monitor_recovered": "Datadog monitor recovered",
    "connection_lost": "Datadog link lost",
    "connection_restored": "Datadog link restored",
}


def _would_notify(tenant_id: str) -> str:
    # Who is told: alerts.alert at warning or info writes an alert_digest row
    # for the main agent of the engine's tenant (the shape older records had).
    return f"main agent alert digest, tenant {tenant_id}"


async def _webhook_covers(tenant: str, monitor_id: str, recovery: bool) -> bool:
    """Whether the Datadog webhook already has this monitor's episode in hand.

    Redis unreadable is ``False``: a duplicate is better than a missed alert.
    """
    from robothor.engine.datadog_delivery import webhook_covers
    from robothor.engine.webhooks import _get_async_redis

    try:
        client = await _get_async_redis()
        if client is None:
            return False
        return await webhook_covers(client, tenant, monitor_id, recovery=recovery)
    except Exception as exc:  # noqa: BLE001 - deliver rather than miss
        logger.info("datadog watch: webhook episodes unreadable (%s)", type(exc).__name__)
        return False


def finding_level(kind: str, details: Mapping[str, Any]) -> str:
    """The alert level of a finding. Nothing the watch finds pages on its own.

    A lost link, a spike, a silent service and a monitor in Alert are
    ``warning``; a monitor in Warn or No Data, and every recovery, ``info``.
    """
    if kind in _CLOSING:
        return "info"
    if kind == "monitor_firing":
        return "warning" if (details.get("monitor") or {}).get("status") == "alert" else "info"
    return "warning"


def _subject(kind: str, details: Mapping[str, Any]) -> str:
    """What an episode is about: one service, one monitor, or the link."""
    if kind.startswith("monitor_"):
        return f"monitor:{(details.get('monitor') or {}).get('id')}"
    if kind.startswith("connection_"):
        return "connection"
    family = "spike" if kind == "error_spike" else "silent"
    return f"{family}:{details.get('service')}"


@dataclass(frozen=True)
class _Firing:
    """A firing monitor as the watch keeps it: the name is already redacted."""

    id: str
    name: str
    type: str
    status: str
    triggered: int | None


class DatadogWatch:
    """The watch's state for one engine: cached slots, firing monitors, silent services, link."""

    def __init__(
        self,
        *,
        home_tenant: str,
        client_factory: Callable[[], Awaitable[AsyncDatadogClient]] = _default_client,
        is_root: Callable[[str], Awaitable[bool]] = _default_is_root,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.home_tenant = home_tenant
        self._client_factory = client_factory
        self._is_root = is_root
        self._clock = clock
        # (tenant, query) -> slot start -> per-service count.
        self._counts: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
        # tenant -> monitor id -> the firing monitor as last recorded.
        self._firing: dict[str, dict[str, _Firing]] = {}
        # tenant -> service -> the start of the slot it went silent in.
        self._silent: dict[str, dict[str, str]] = {}
        self._outcomes: deque[str] = deque(maxlen=CONNECTION_FAILURES_FOR_LOST + 1)
        self._lost = False
        self._said: set[str] = set()
        # (tenant, subject) -> the highest level sent in its open episode.
        self._episodes: dict[tuple[str, str], str] = {}
        # (tenant, subject) -> an episode's opening finding whose alert failed:
        # tried again on each pass that still sees the condition.
        self._failed: dict[tuple[str, str], tuple[str, str, dict[str, Any], str | None]] = {}

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
        observers = (
            ("error spike", self._watch_spikes),
            ("silent service", self._watch_silence),
            ("monitor", self._watch_monitors),
        )
        recorded = 0
        for name, observer in observers:
            try:
                recorded += await observer(client, scope, windows, failures)
            except DatadogError as exc:
                failures.append(exc.kind)
                logger.info(
                    "datadog watch: tenant %s %s watch skipped this tick (%s)",
                    tenant,
                    name,
                    exc.kind,
                )
            except Exception as exc:  # noqa: BLE001 - one observer never stops the others
                logger.warning(
                    "datadog watch: tenant %s %s watch failed this tick (%s)",
                    tenant,
                    name,
                    type(exc).__name__,
                )
        return recorded

    async def _series(
        self,
        client: AsyncDatadogClient,
        scope: DatadogScope,
        query: str,
        windows: list[tuple[datetime, datetime]],
    ) -> tuple[dict[str, int], dict[str, list[int]]]:
        """Per-service counts of ``query`` in the current slot, and in each baseline slot.

        Each settled slot is read once and kept; slots that left the windows
        are dropped. A service missing from a slot counted zero there.
        """
        cache = self._counts.setdefault((scope.tenant_id, query), {})
        keep = {_iso(start) for start, _ in windows}
        for stale in [k for k in cache if k not in keep]:
            del cache[stale]
        counts = [await self._count(client, scope, query, cache, s, e) for s, e in windows]
        current, history = counts[0], counts[1:]
        seen = {service for window in history for service in window}
        return current, {s: [w.get(s, 0) for w in history] for s in seen}

    @staticmethod
    async def _count(
        client: AsyncDatadogClient,
        scope: DatadogScope,
        query: str,
        cache: dict[str, dict[str, int]],
        start: datetime,
        end: datetime,
    ) -> dict[str, int]:
        key = _iso(start)
        if key not in cache:
            buckets = await client.aggregate_logs(
                scope,
                query,
                start=_iso(start),
                end=_iso(end),
                group_by=["service"],
                limit=GROUP_PAGE,
            )
            per_service: dict[str, int] = {}
            for bucket in buckets:
                service = bucket.by.get("service") or _NO_SERVICE
                per_service[service] = per_service.get(service, 0) + bucket.count
            cache[key] = per_service
        return cache[key]

    async def _watch_spikes(
        self,
        client: AsyncDatadogClient,
        scope: DatadogScope,
        windows: list[tuple[datetime, datetime]],
        failures: list[str],
    ) -> int:
        current, baseline = await self._series(client, scope, "status:error", windows)
        start, end = windows[0]
        findings = check_error_spike(current, baseline)
        # A spike no longer found has ended: its episode closes, quietly.
        spiking = {f"spike:{f.service}" for f in findings}
        for key in [k for k in self._episodes if k[0] == scope.tenant_id]:
            if key[1].startswith("spike:") and key[1] not in spiking:
                del self._episodes[key]
        recorded = 0
        fresh: set[str] = set()
        for finding in findings:
            if await self._spike(scope.tenant_id, finding, start, end):
                recorded += 1
                fresh.add(f"spike:{finding.service}")
        # A spike found afresh this pass replaced its stored finding: one send.
        return recorded + await self._retry(
            scope.tenant_id, "spike:", lambda s: s in spiking, skip=fresh
        )

    async def _watch_silence(
        self,
        client: AsyncDatadogClient,
        scope: DatadogScope,
        windows: list[tuple[datetime, datetime]],
        failures: list[str],
    ) -> int:
        tenant = scope.tenant_id
        # An empty query counts every line inside the scope.
        current, baseline = await self._series(client, scope, "", windows)
        if len(current) >= GROUP_PAGE:
            # A full group page may have left services out; absent is not silent.
            self._once(
                f"silence-page:{tenant}",
                "datadog watch: tenant %s logs from %d+ services; silence not judged",
                tenant,
                GROUP_PAGE,
            )
            return 0
        silent = self._silent.setdefault(tenant, {})
        retried = await self._retry(
            tenant, "silent:", lambda s: s[7:] in silent and not current.get(s[7:], 0)
        )
        start, end = windows[0]
        hours = BASELINE_WINDOWS * WINDOW_MINUTES // 60
        when = f"the {WINDOW_MINUTES} minutes to {end.strftime('%H:%M')} UTC"
        window = {"start": _iso(start), "end": _iso(end), "minutes": WINDOW_MINUTES}
        recorded = 0
        for change in check_silence(current, baseline, silent):
            service = change.service
            if change.kind == "silent":
                silent[service] = _iso(start)
                brief = (
                    f"{service} logged nothing in {when}, after logging in every "
                    f"{WINDOW_MINUTES} minutes of the {hours} hours before (usually "
                    f"{change.baseline_median:g} lines). Check that it is running and still "
                    "shipping logs to Datadog."
                )
                details: dict[str, Any] = {
                    "service": service,
                    "current": 0,
                    "baseline": {"median": change.baseline_median, "windows": BASELINE_WINDOWS},
                    "window": window,
                }
                await self._finding(tenant, "service_silent", brief, details)
            else:
                since = silent.pop(service)
                brief = (
                    f"{service} is logging again: {change.current} lines in {when}, "
                    f"after going silent in the slot from {since[11:16]} UTC."
                )
                details = {
                    "service": service,
                    "current": change.current,
                    "silent_since": since,
                    "window": window,
                }
                await self._finding(tenant, "service_resumed", brief, details)
            recorded += 1
        return recorded + retried

    async def _watch_monitors(
        self,
        client: AsyncDatadogClient,
        scope: DatadogScope,
        windows: list[tuple[datetime, datetime]],
        failures: list[str],
    ) -> int:
        tenant = scope.tenant_id
        try:
            firing = await self._firing_monitors(client, scope)
        except ScopeError as exc:
            self._once(
                f"monitors:{tenant}",
                "datadog watch: tenant %s monitor watch skipped (%s)",
                tenant,
                exc.reason,
            )
            return 0
        except DatadogError as exc:
            # A key without monitor read access still reads logs: only an
            # unreachable Datadog counts toward a lost link from here.
            if exc.kind == "network":
                failures.append(exc.kind)
            logger.info(
                "datadog watch: tenant %s monitor watch skipped this tick (%s)", tenant, exc.kind
            )
            return 0
        recorded_now = self._firing.setdefault(tenant, {})
        statuses = {mid: f.status for mid, f in firing.items()}
        before = {mid: f.status for mid, f in recorded_now.items()}
        now = self._clock()
        # Still firing at the status that failed to go out: try it again. A
        # status change makes a new finding below instead.
        recorded = await self._retry(
            tenant,
            "monitor:",
            lambda s: statuses.get(s[8:]) is not None and statuses.get(s[8:]) == before.get(s[8:]),
        )
        for change in check_monitors(statuses, before):
            if change.kind == "firing":
                monitor = firing[change.monitor_id]
                recorded_now[change.monitor_id] = monitor
                shown = _FIRING[monitor.status]
                moved = f", moved from {_FIRING[change.previous]}" if change.previous else ""
                brief = (
                    f'Datadog monitor "{monitor.name}" is in {shown}'
                    f"{_since(monitor.triggered, now)}{moved}. Check it in Datadog."
                )
                kind = "monitor_firing"
            else:
                monitor = recorded_now.pop(change.monitor_id)
                brief = (
                    f'Datadog monitor "{monitor.name}" is no longer firing, or has been '
                    f"muted (it was in {_FIRING[monitor.status]})."
                )
                kind = "monitor_recovered"
            details = {
                "service": None,
                "monitor": {
                    "id": monitor.id,
                    "name": monitor.name,
                    "type": monitor.type,
                    "status": change.status,
                    "previous_status": change.previous,
                },
            }
            await self._finding(tenant, kind, brief, details, target=f"monitor:{monitor.id}")
            recorded += 1
        return recorded

    @staticmethod
    async def _firing_monitors(
        client: AsyncDatadogClient, scope: DatadogScope
    ) -> dict[str, _Firing]:
        """Every monitor in the scope now in Alert, Warn or No Data, by id; every page read."""
        firing: dict[str, _Firing] = {}
        page = 0
        while True:
            result = await client.search_monitors(
                scope, FIRING_QUERY, limit=MONITOR_PAGE, page=page
            )
            for row in result.rows:
                status = row.status.strip().lower()
                if row.id and status in _FIRING:
                    firing[row.id] = _Firing(
                        row.id,
                        redact_personal_data(row.name),
                        row.type,
                        status,
                        row.last_triggered_ts,
                    )
            page += 1
            if not result.rows or page >= result.page_count:
                return firing

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
        await self._finding(tenant, "error_spike", brief, details)
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
            return await self._retry(self.home_tenant, "connection", lambda _s: self._lost)
        tenant = self.home_tenant
        if verdict == "lost":
            self._lost = True
            cause = (
                f"Datadog rejected {rejected}"
                if self._outcomes[-1] == "auth"
                else "Datadog could not be reached"
            )
            brief = (
                f"Genus has lost its Datadog link: {cause} on the last "
                f"{CONNECTION_FAILURES_FOR_LOST} checks. Services and monitors are not being "
                "watched until it works again; run `genus doctor --only datadog.reachable`."
            )
            kind = "connection_lost"
        else:
            self._lost = False
            brief = "Genus can read Datadog again. Watching services and monitors has resumed."
            kind = "connection_restored"
        details = {"service": None, "checks": list(self._outcomes)}
        self._outcomes.clear()
        await self._finding(tenant, kind, brief, details)
        return 1

    async def _deliver(
        self, tenant: str, kind: str, brief: str, details: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Send a finding once per episode; ``{"mode", "delivery"}``. Never raises."""
        from robothor.engine.datadog_delivery import FAILED, SUPPRESSED, send_alert

        if tenant != self.home_tenant:
            # The alerts path delivers in this engine's tenant only.
            return {"mode": FAILED, "delivery": {"reason": "not_this_engine"}}
        level = finding_level(kind, details)
        subject = _subject(kind, details)
        key = (tenant, subject)
        before = self._episodes.get(key)
        monitor_id = (details.get("monitor") or {}).get("id")
        if monitor_id and await _webhook_covers(tenant, str(monitor_id), kind in _CLOSING):
            # The webhook sent this monitor's episode: not again from here.
            if kind in _CLOSING:
                self._episodes.pop(key, None)
            return {
                "mode": SUPPRESSED,
                "delivery": {"level": level, "reason": "covered_by_webhook"},
            }
        if kind in _CLOSING:
            self._episodes.pop(key, None)
            phase = "close"
        elif before is not None and _RANK[level] <= _RANK[before]:
            return {"mode": SUPPRESSED, "delivery": {"level": level, "reason": "episode_open"}}
        else:
            self._episodes[key] = level
            phase = f"open:{level}"
        name = (details.get("monitor") or {}).get("name") or details.get("service")
        title = f"{_TITLES.get(kind, 'Datadog')}: {name}" if name else _TITLES.get(kind, kind)
        outcome = await send_alert(
            level,
            title,
            brief,
            alert_id=f"datadog_watch:{tenant}:{subject}:{phase}",
            kind=f"datadog_{kind}",
            metadata={"tenant": tenant, "kind": kind},
        )
        if outcome["mode"] == FAILED and kind not in _CLOSING:
            # Nobody heard: the next finding of this episode tries again.
            if before is None:
                self._episodes.pop(key, None)
            else:
                self._episodes[key] = before
        return outcome

    async def _retry(
        self,
        tenant: str,
        prefix: str,
        still: Callable[[str], bool],
        *,
        skip: Collection[str] = (),
    ) -> int:
        """Try again the failed alerts of ``tenant`` under ``prefix`` whose condition lasts.

        ``skip``: subjects already sent afresh this pass, whose stored finding
        that fresh one replaced.
        """
        tried = 0
        for key in [k for k in self._failed if k[0] == tenant and k[1].startswith(prefix)]:
            if key[1] in skip:
                continue
            kind, brief, details, target = self._failed[key]
            if not still(key[1]):
                del self._failed[key]
                continue
            await self._finding(tenant, kind, brief, details, target=target)
            tried += 1
        return tried

    async def _finding(
        self,
        tenant: str,
        kind: str,
        brief: str,
        details: dict[str, Any],
        *,
        target: str | None = None,
    ) -> None:
        """Deliver one finding, then record it with what the delivery did.

        An episode's opening finding whose alert failed is kept, and tried
        again (same alert id) by each pass that still sees the condition,
        until it is delivered or the condition ends.
        """
        from robothor.integrations.datadog.alerts import outcome_reason

        target = target or details.get("service") or None
        outcome = await self._deliver(tenant, kind, brief, details)
        mode = outcome["mode"]
        key = (tenant, _subject(kind, details))
        retryable = "error" in outcome["delivery"]
        if mode == "delivery_failed" and retryable and kind not in _CLOSING:
            self._failed[key] = (kind, brief, details, target)
        else:
            self._failed.pop(key, None)
        event = {
            "kind": kind,
            **details,
            "mode": mode,
            **outcome_reason(outcome),
            "delivery": outcome["delivery"],
            # Kept from the records before delivery, for their readers.
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
            target=target,
            tenant_id=tenant,
        )
        fields: dict[str, Any] = {"tenant": tenant, "kind": kind, "service": details.get("service")}
        if target != fields["service"]:
            fields["target"] = target
        logger.warning("%s", kv_line("datadog_watch", **fields, mode=mode))


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
