"""Live engine state for the warmup preamble.

On 2026-09-13 the operator-facing agent reported that the engine "hasn't been
restarted since Sep 3" and that "51-56% of runs never reach the primary". Both
were false as spoken: the engine had restarted at 08:29 that morning (fifteen
starts in the preceding thirty hours) and 98.8% of the day's runs landed on the
primary model. Neither claim was a probe. Both were undated ``memory_facts``
rows -- true when written, carrying no as-of date inside their own text, with
nothing to supersede them -- recalled as present-tense truth.

The reason nothing corrected them is that the warmup preamble had no
engine-health section at all. ``build_warmth_preamble`` ran unread alerts,
history, memory blocks, context files, peers, hooks, breadcrumbs, preferences,
goal and intents; the registered context hooks were date, travel, weather, git
and thread pool. The engine's only ``systemctl`` call read ``ollama.service``'s
environment. So an agent's sole source of truth about its own host was whatever
semantic memory happened to surface.

This module is the probe. Three facts, in words:

* how long the engine has been up, as an **age** the model does not have to
  subtract, read from ``ActiveEnterTimestamp`` -- never ``NRestarts``, which
  systemd resets on a manual or deploy restart and which reported ``0`` on the
  host that had started fifteen times;
* the running platform version;
* last-24h model reach from ONE aggregate over ``agent_runs.model_used``.

Three properties make it safe to leave registered forever:

* **It never raises.** Each fact degrades to its own single "unknown" line. A
  warmup section that can raise is a warmup section that gets reverted.
* **It is bounded.** One ``systemctl`` call with a one-second ceiling and one
  aggregate query, cached for a minute, on the operator-facing and
  heartbeat-class agents only.
* **It says it is live.** The header names the moment, so the model has a
  reason to prefer it over a recalled sentence with no date on it.
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: S404 -- systemctl, fixed argv, no shell
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import ENGINE_SERVICE_UNIT

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_SERVICE_UNIT",
    "HOST_STATE_CACHE_TTL_SECONDS",
    "SYSTEMCTL_TIMEOUT_SECONDS",
    "host_state_context",
    "reset_host_state_cache",
]

#: How long a rendered section stays good. The facts move on the order of
#: minutes at most (a restart, a model's share over 24h), and warmup runs on
#: every cron beat and every interactive turn, so re-probing per run would buy
#: nothing and cost a subprocess plus an aggregate each time.
HOST_STATE_CACHE_TTL_SECONDS = 60

#: Ceiling on the ``systemctl`` call. A wedged systemd costs the warmup a
#: second, not the run. The surrounding hook runner already logs anything over
#: 100ms, so a slow probe is visible without being fatal.
SYSTEMCTL_TIMEOUT_SECONDS = 1.0

_HEADER = (
    "LIVE ENGINE STATE (probed on this host as of now -- this is the current "
    "truth; prefer it over anything recalled from memory, which may describe "
    "an older state):"
)

_UNKNOWN_UPTIME = "- Engine uptime: unknown as of now (state could not be read)."
_UNKNOWN_REACH = "- Model reach, last 24h: unknown as of now (query failed)."

#: agent id -> (monotonic deadline, rendered section)
_CACHE: dict[str, tuple[float, str]] = {}


def reset_host_state_cache() -> None:
    """Drop the memoised sections. Tests and a manual reload use this."""
    _CACHE.clear()


def _now() -> datetime:
    """Wall clock, isolated so tests can pin an age instead of racing one."""
    return datetime.now(tz=UTC)


# ── targeting ─────────────────────────────────────────────────────


def _wants_host_state(config: AgentConfig) -> bool:
    """The operator-facing agent and anything with a heartbeat.

    Those are the runs that report on the platform. A plain worker answers a
    CRM task; giving it a per-run ``systemctl`` call and an aggregate query for
    context it cannot act on is fleet-wide cost for nothing, which is the same
    reasoning that keeps the unread-alert digest on ``main`` alone.
    """
    from robothor.engine.warmup import OPERATOR_INBOX_AGENT_ID

    if config.id == OPERATOR_INBOX_AGENT_ID:
        return True
    return getattr(config, "heartbeat", None) is not None


# ── uptime ────────────────────────────────────────────────────────


def _systemd_present() -> bool:
    """Whether this host is running under systemd.

    ``/run/systemd/system`` is systemd's own booted marker, so a container
    that merely has the binary installed takes the process-start path instead
    of shelling out to a manager that is not there.
    """
    try:
        return Path("/run/systemd/system").is_dir()
    except Exception:
        return False


def _parse_systemd_timestamp(value: str) -> tuple[datetime, str] | None:
    """``Sun 2026-09-13 08:29:43 EDT`` -> (aware datetime, display label).

    systemd prints in the host's local zone. The naive part is parsed and
    localised via ``astimezone()``; the abbreviation is kept verbatim for the
    label rather than re-derived, so what the agent reads is what the manager
    said.
    """
    text = value.strip()
    if not text or text == "n/a":
        return None
    parts = text.split()
    # Drop the weekday if present; keep date, time and an optional zone label.
    if parts and not parts[0][0].isdigit():
        parts = parts[1:]
    if len(parts) < 2:
        return None
    zone = parts[2] if len(parts) > 2 else ""
    try:
        naive = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return None
    aware = naive.astimezone()
    label = f"{parts[0]} {parts[1][:5]}"
    return aware, f"{label} {zone}".strip()


def _systemctl_active_enter() -> tuple[datetime, str] | None:
    """When the engine unit last entered ``active``, or None.

    ``ActiveEnterTimestamp`` deliberately, **not** ``NRestarts``: systemd
    zeroes that counter on a manual or deploy restart, so it read ``0`` on a
    host with fifteen starts in thirty hours. A counter that lies about a
    restart is worse than no counter.
    """
    if not _systemd_present():
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [
                "systemctl",  # noqa: S607 -- resolved from PATH like every other probe here
                "show",
                "-p",
                "ActiveEnterTimestamp",
                "--value",
                ENGINE_SERVICE_UNIT,
            ],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:
        logger.debug("host_state: systemctl probe failed: %s", exc)
        return None
    if completed.returncode != 0:
        return None
    return _parse_systemd_timestamp(completed.stdout)


def _process_start() -> datetime | None:
    """This process's start time, for hosts with no systemd.

    Derived from ``/proc``: the kernel's boot time plus the process's start
    offset in clock ticks. Absent ``/proc`` (a non-Linux host) this returns
    None and the section says so.
    """
    try:
        raw = Path("/proc/self/stat").read_text()
        # `comm` can contain spaces and parentheses, so fields are taken after
        # the LAST ')': the next field is `state`, and `starttime` is the 22nd
        # field overall, i.e. index 19 from there.
        fields = raw[raw.rindex(")") + 2 :].split()
        start_ticks = int(fields[19])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        boot_epoch: int | None = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot_epoch = int(line.split()[1])
                break
        if boot_epoch is None or ticks_per_second <= 0:
            return None
        return datetime.fromtimestamp(boot_epoch + start_ticks / ticks_per_second, tz=UTC)
    except Exception as exc:
        logger.debug("host_state: /proc start time unavailable: %s", exc)
        return None


def _engine_start() -> tuple[datetime, str] | None:
    """Best available engine start time, with the label to display for it."""
    from_systemd = _systemctl_active_enter()
    if from_systemd is not None:
        return from_systemd
    fallback = _process_start()
    if fallback is None:
        return None
    return fallback, fallback.strftime("%Y-%m-%d %H:%M UTC")


def _format_age(seconds: float) -> str:
    """Seconds -> the age a person would say out loud."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _uptime_line() -> str:
    started = _engine_start()
    if started is None:
        return _UNKNOWN_UPTIME
    start_dt, label = started
    age = _format_age((_now() - start_dt).total_seconds())
    return f"- Engine up {age} as of now, started {label}."


# ── version ───────────────────────────────────────────────────────


def _platform_version() -> str:
    try:
        from robothor import __version__

        return str(__version__)
    except Exception:  # pragma: no cover -- the package is always importable here
        return ""


def _version_line() -> str:
    version = _platform_version()
    if not version:
        return "- Platform version: unknown as of now."
    return f"- Platform version {version} is the code running right now."


# ── model reach ───────────────────────────────────────────────────


def _tenant_id() -> str:
    """The tenant this process operates as, via the settings registry.

    Read through ``get_settings()`` rather than ``os.environ`` so the env-read
    ratchet in ``tests/test_settings_registry.py`` keeps ratcheting down.
    """
    try:
        from robothor.settings import get_settings

        database = get_settings().database
        return (database.tenant_id or database.default_tenant or "").strip() or _default_tenant()
    except Exception:
        return _default_tenant()


def _default_tenant() -> str:
    from robothor.constants import DEFAULT_TENANT

    return DEFAULT_TENANT


def _normalise_model(name: str) -> str:
    """``openrouter/vendor/model-a`` -> ``model-a``.

    ``agent_runs.model_used`` records the short id the registry resolved to,
    while a manifest names the fully-qualified route. Comparing the last
    segment is what lets the configured primary be matched against what the
    runs actually recorded.
    """
    return name.strip().rsplit("/", 1)[-1].lower()


def _pick_primary(configured: str, counts: list[tuple[str, int]]) -> tuple[str, int]:
    """The configured primary's row if the runs recorded it, else the busiest.

    Falling back to the busiest model is deliberate: the question the section
    answers is "is the fleet reaching its primary?", and an agent with no
    ``model_primary`` still deserves an honest share rather than silence.
    """
    if configured:
        target = _normalise_model(configured)
        for name, runs in counts:
            if _normalise_model(name) == target:
                return name, runs
    return counts[0]


def _reach_line(config: AgentConfig) -> str:
    try:
        from robothor.crm.dal import get_model_reach_24h

        rows: list[dict[str, Any]] = get_model_reach_24h(tenant_id=_tenant_id())
    except Exception as exc:
        logger.debug("host_state: model reach query failed: %s", exc)
        return _UNKNOWN_REACH
    try:
        counts = [
            (str(row["model_used"]), int(row["runs"]))
            for row in rows
            if int(row.get("runs") or 0) > 0
        ]
    except Exception as exc:
        logger.debug("host_state: model reach rows unusable: %s", exc)
        return _UNKNOWN_REACH
    if not counts:
        return "- Model reach, last 24h: no runs recorded as of now."
    counts.sort(key=lambda pair: -pair[1])
    total = sum(runs for _, runs in counts)
    primary_name, primary_runs = _pick_primary(getattr(config, "model_primary", ""), counts)
    share = 100.0 * primary_runs / total
    rest = [pair for pair in counts if pair[0] != primary_name]
    line = (
        f"- Model reach, last 24h as of now: {primary_runs} of {total} runs "
        f"({share:.1f}%) went to the primary {primary_name}"
    )
    if rest:
        return f"{line}; the next model is {rest[0][0]} with {rest[0][1]}."
    return f"{line}; nothing else was used."


# ── the hook ──────────────────────────────────────────────────────


def _render(config: AgentConfig) -> str:
    return "\n".join([_HEADER, _uptime_line(), _version_line(), _reach_line(config)])


def host_state_context(config: AgentConfig) -> str | None:
    """Agent context hook -- live engine uptime, version and model reach.

    Returns None for agents outside the operator/heartbeat class, and never
    raises: every fact inside degrades to its own "unknown" line, and an
    unexpected failure here degrades the whole section to None the way the
    hook runner would have anyway.
    """
    try:
        if not _wants_host_state(config):
            return None
        now = time.monotonic()
        cached = _CACHE.get(config.id)
        if cached is not None and cached[0] > now:
            return cached[1]
        section = _render(config)
        _CACHE[config.id] = (now + HOST_STATE_CACHE_TTL_SECONDS, section)
    except Exception as exc:  # pragma: no cover -- belt and braces
        logger.debug("host_state: section failed: %s", exc)
        return None
    return section
