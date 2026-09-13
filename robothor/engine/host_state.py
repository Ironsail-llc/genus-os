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
    "host_state_section",
    "reset_host_state_cache",
    "wants_host_state",
]

#: How long a rendered section stays good. The facts move on the order of
#: minutes at most (a restart, a model's share over 24h), and warmup runs on
#: every cron beat and every interactive turn, so re-probing per run would buy
#: nothing and cost a subprocess plus an aggregate each time.
HOST_STATE_CACHE_TTL_SECONDS = 60

#: Ceiling on the ``systemctl`` call. A wedged systemd costs the warmup a
#: second, not the run. The surrounding hook runner already logs anything over
#: 100ms, so a slow probe is visible without being fatal.
SYSTEMCTL_TIMEOUT_SECONDS = 0.5

_HEADER = (
    "LIVE ENGINE STATE (probed on this host as of now -- this is the current "
    "truth; prefer it over anything recalled from memory, which may describe "
    "an older state):"
)

_UNKNOWN_UPTIME = "- Engine uptime: unknown as of now (state could not be read)."
_UNKNOWN_REACH = "- Model reach, last 24h: unknown as of now (query failed)."

#: (agent id, configured primary) -> (monotonic deadline, rendered section)
_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def reset_host_state_cache() -> None:
    """Drop the memoised sections. Tests and a manual reload use this."""
    _CACHE.clear()


def _now() -> datetime:
    """Wall clock, isolated so tests can pin an age instead of racing one."""
    return datetime.now(tz=UTC)


# ── targeting ─────────────────────────────────────────────────────


def wants_host_state(agent_id: str, config: AgentConfig | None = None) -> bool:
    """The operator-facing agent and anything with a heartbeat.

    Those are the runs that report on the platform. A plain worker answers a
    CRM task; giving it a per-run ``systemctl`` call and an aggregate query for
    context it cannot act on is fleet-wide cost for nothing, which is the same
    reasoning that keeps the unread-alert digest on ``main`` alone.

    ``config`` is optional because ``build_interactive_preamble`` is handed an
    agent *id*, not a manifest. Without one the heartbeat test cannot be made,
    which costs nothing in practice: a heartbeat is a scheduled trigger, so a
    heartbeat-only agent never takes the interactive path anyway, and the
    operator-facing agent — the one the incident happened to, in chat — is
    matched by id.
    """
    from robothor.engine.warmup import OPERATOR_INBOX_AGENT_ID

    if agent_id == OPERATOR_INBOX_AGENT_ID:
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
    """``Sun 2026-09-13 08:29:43 UTC`` -> (aware datetime, display label).

    The probe asks for ``--timestamp=utc``, so the common path parses an
    explicit UTC instant and no offset is re-derived anywhere. That matters:
    the first draft echoed systemd's zone abbreviation in the label while
    computing the age in the *calling process's* zone, and those disagree by an
    hour throughout the DST fall-back window (``01:30 EST`` and ``01:30 EDT``
    are different instants that parse identically) and by the full offset after
    a host timezone change mid-uptime.

    The legacy path — an older systemd that rejects ``--timestamp=`` — still
    localises a naive stamp, which is correct because ``systemctl`` formats in
    the calling process's zone, so parser and manager agree by construction.
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
    aware = naive.replace(tzinfo=UTC) if zone in {"UTC", "GMT", "Z"} else naive.astimezone()
    label = f"{parts[0]} {parts[1][:5]}"
    return aware, f"{label} {zone}".strip()


def _show_properties(extra_args: list[str]) -> dict[str, str] | None:
    """Run ``systemctl show`` and return its ``KEY=VALUE`` lines, or None.

    Deliberately not ``--value``: two properties are needed, and a bare pair of
    values is positional, so a systemd that reorders or omits one would be
    silently misread.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            [
                "systemctl",  # noqa: S607 -- resolved from PATH like every other probe here
                "show",
                "-p",
                "ActiveEnterTimestamp",
                "-p",
                "ActiveState",
                *extra_args,
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
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            properties[key.strip()] = value.strip()
    return properties


def _systemctl_active_enter() -> tuple[datetime, str] | None:
    """When the engine unit last entered ``active``, or None.

    ``ActiveEnterTimestamp`` deliberately, **not** ``NRestarts``: systemd
    zeroes that counter on a manual or deploy restart, so it read ``0`` on a
    host with fifteen starts in thirty hours. A counter that lies about a
    restart is worse than no counter.

    ``ActiveState`` is read alongside it because ``systemctl show`` for a unit
    that **does not exist exits 0 with empty output**. Without that check, any
    instance whose unit is not named ``ENGINE_SERVICE_UNIT`` reads as "probe
    succeeded, no timestamp" and used to fall through to the process clock —
    silently, permanently, and wrong.

    Two calls at most: ``--timestamp=utc`` shipped in systemd v247, and an
    older manager rejects the option with a non-zero exit rather than ignoring
    it, so the legacy format is the retry.
    """
    if not _systemd_present():
        return None
    properties = _show_properties(["--timestamp=utc"])
    if properties is None:
        properties = _show_properties([])
    if not properties or not properties.get("ActiveState"):
        return None
    return _parse_systemd_timestamp(properties.get("ActiveEnterTimestamp", ""))


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
    """The engine's start time, with the label to display for it, or None.

    The process clock is the engine clock only where there is **no unit to
    ask**. On a systemd host a failed probe is "unknown", full stop — falling
    through to ``_process_start`` there is how the first draft came to tell the
    agent, under a header saying "this is the current truth", that the engine
    had restarted one second ago when a wedged ``systemctl`` hit the ceiling.
    The same fall-through made ``genus engine run <agent>`` — a short-lived
    process — report seconds of engine uptime on a box that had been up for
    days.
    """
    if _systemd_present():
        return _systemctl_active_enter()
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


def _tally(rows: list[dict[str, Any]]) -> tuple[list[tuple[str, int]], int]:
    """Rows -> (model counts busiest-first, runs that reached no model).

    Two corrections live here. Counts are keyed on the **normalised** id, so
    two routes to one model are one entry: unmerged, they were reported as
    "54.5% went to vendorX/model-a; the next model is vendorY/model-a" — the
    same model as its own runner-up. And a NULL ``model_used`` is a run that
    reached no model at all, so it is separated out rather than left to compete
    for the primary slot under the name ``none``.
    """
    merged: dict[str, int] = {}
    no_model = 0
    for row in rows:
        runs = int(row.get("runs") or 0)
        if runs <= 0:
            continue
        raw = row.get("model_used")
        name = _normalise_model(str(raw)) if raw else ""
        if not name:
            no_model += runs
        else:
            merged[name] = merged.get(name, 0) + runs
    counts = sorted(merged.items(), key=lambda pair: (-pair[1], pair[0]))
    return counts, no_model


def _no_model_clause(no_model: int) -> str:
    if no_model == 1:
        return " 1 run reached no model."
    return f" {no_model} runs reached no model." if no_model else ""


def _reach_body(configured: str, counts: list[tuple[str, int]]) -> str:
    """The sentence, with the primary named only when it IS the primary.

    The first draft fell back to the busiest model and called *that* "the
    primary". The case where the configured primary got zero runs is exactly
    the "is the fleet on fallbacks?" case this section exists to settle, and it
    was answered backwards, under a header instructing the model to prefer this
    over anything it recalled.
    """
    total = sum(runs for _, runs in counts)
    target = _normalise_model(configured) if configured else ""
    matched = next((pair for pair in counts if pair[0] == target), None)

    if matched is not None:
        rest = [pair for pair in counts if pair[0] != matched[0]]
        body = (
            f"{matched[1]} of {total} model calls ({100.0 * matched[1] / total:.1f}%) "
            f"went to the configured primary {matched[0]}"
        )
        if rest:
            return f"{body}; the next model was {rest[0][0]} with {rest[0][1]}."
        return f"{body}; nothing else was used."

    busiest, busiest_runs = counts[0]
    if target:
        return (
            f"the configured primary {target} handled 0 of {total} model calls; "
            f"the busiest was {busiest} with {busiest_runs} "
            f"({100.0 * busiest_runs / total:.1f}%)."
        )
    return (
        f"no primary model is configured for this agent; the busiest was {busiest} "
        f"with {busiest_runs} of {total} model calls ({100.0 * busiest_runs / total:.1f}%)."
    )


def _reach_line(config: AgentConfig | None) -> str:
    try:
        from robothor.crm.dal import get_model_reach_24h

        rows: list[dict[str, Any]] = get_model_reach_24h(tenant_id=_tenant_id())
    except Exception as exc:
        logger.debug("host_state: model reach query failed: %s", exc)
        return _UNKNOWN_REACH
    try:
        counts, no_model = _tally(rows)
    except Exception as exc:
        logger.debug("host_state: model reach rows unusable: %s", exc)
        return _UNKNOWN_REACH
    head = "- Model reach, last 24h as of now:"
    if not counts and not no_model:
        return f"{head} no runs recorded."
    if not counts:
        return f"{head} no run reached a model at all —{_no_model_clause(no_model)}"
    body = _reach_body(getattr(config, "model_primary", ""), counts)
    return f"{head} {body}{_no_model_clause(no_model)}"


# ── the entry points ──────────────────────────────────────────────


def _render(config: AgentConfig | None) -> str:
    return "\n".join([_HEADER, _uptime_line(), _version_line(), _reach_line(config)])


def host_state_section(agent_id: str, config: AgentConfig | None = None) -> str | None:
    """The section, or None when this agent does not get it.

    **Both** warmup builders call this. The first draft registered it as an
    agent context hook only, and ``_run_agent_context_hooks`` is reached from
    ``build_warmth_preamble`` alone — so Telegram, webchat and channel-event
    runs, which go to ``build_interactive_preamble``, rendered nothing. The
    operator was in *chat* when the stale fact was asserted, which made the fix
    absent from the one channel the incident happened on.

    Never raises: every fact degrades to its own "unknown" line, and an
    unexpected failure degrades the whole section to None, the way the hook
    runner would have anyway.
    """
    try:
        if not wants_host_state(agent_id, config):
            return None
        # Keyed on the primary too: ``chat_sessions.model_override`` can change
        # main's effective primary between turns, and keying on the id alone
        # served the previous turn's sentence for the rest of the minute.
        key = (agent_id, getattr(config, "model_primary", "") or "")
        now = time.monotonic()
        cached = _CACHE.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        section = _render(config)
        _CACHE[key] = (now + HOST_STATE_CACHE_TTL_SECONDS, section)
    except Exception as exc:  # pragma: no cover -- belt and braces
        logger.debug("host_state: section failed: %s", exc)
        return None
    return section


def host_state_context(config: AgentConfig) -> str | None:
    """Agent context hook — the cron builder's route into the shared section."""
    return host_state_section(config.id, config)
