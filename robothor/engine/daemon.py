"""
Main daemon entry point — starts all engine subsystems.

Runs as: python -m robothor.engine.daemon

Subsystems:
- Telegram bot (long-polling)
- Cron scheduler (APScheduler)
- Event hooks (Redis Stream consumers)
- Health endpoint (FastAPI on port 18800)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.engine.config import EngineConfig
from robothor.engine.health import serve_health, validate_engine_auth_configuration
from robothor.engine.hooks import EventHooks
from robothor.engine.runner import AgentRunner
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.scheduler import CronScheduler
from robothor.engine.telegram import TelegramBot
from robothor.engine.workflow import WorkflowEngine
from robothor.plugins import reload_plugins

if TYPE_CHECKING:
    from robothor.engine.resume import ResumeCandidate

logger = logging.getLogger(__name__)


def _sd_notify(state: str) -> None:
    """Send a notification to systemd via $NOTIFY_SOCKET (sd_notify protocol).

    No-ops silently if NOTIFY_SOCKET is not set or the socket is unreachable.
    Uses stdlib only — no external dependencies.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        # Abstract socket (starts with @) or filesystem path
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(state.encode(), addr)
        finally:
            sock.close()
    except Exception as e:  # noqa: BLE001 - never crash the daemon for sd_notify
        # Best-effort, but not invisible: losing sd_notify means systemd stops
        # seeing the watchdog ping and will eventually restart the engine, so
        # the reason belongs in the log that gets read afterwards.
        logger.debug("sd_notify failed (%s): %s", state, e)


# Set on daemon startup so the reaper can distinguish runs killed by a daemon
# restart from runs where the runner process itself crashed. ISO8601 string.
_DAEMON_START_TS: str | None = None

#: Head-room between a run's own wall-clock ceiling and the moment the reaper
#: is willing to call it dead. Sized to cover FINALIZATION_TOTAL_BUDGET (a run
#: may still be writing its summary after the loop ends) plus row-write latency.
REAP_GRACE_SECONDS = 300


#: Cheap floor for the candidate scan. The real decision is per-agent below;
#: this only keeps the query from returning every young row.
REAP_MIN_SCAN_SECONDS = 300


def _is_orphan(started_at: Any, daemon_start_ts: str | None) -> bool:
    """Did this run start before the current daemon booted?

    If so nothing is executing it, whatever its age, and it can be reaped at
    once — strictly faster than the flat 30 minutes this replaced. 60s of slack
    so a run started during boot is not mistaken for one that outlived the
    previous daemon. Unknown timestamps are NOT orphans: the age-based gate is
    the safe default, because guessing here destroys live work.
    """
    if not daemon_start_ts or started_at is None:
        return False
    try:
        boot = datetime.fromisoformat(daemon_start_ts)
        if boot.tzinfo is None:
            boot = boot.replace(tzinfo=UTC)
        return bool(started_at < boot - timedelta(seconds=60))
    except Exception:  # noqa: BLE001 - a malformed stamp must not reap anything
        return False


def stale_run_cutoff_seconds(agent_id: str | None = None) -> int:
    """How old a LIVE `running` row must be before the reaper touches it.

    Previously a hardcoded 30 minutes. On the local tier `main`'s SUCCESSFUL
    runs average 33.5 minutes and reach 47.3, so the reaper was tombstoning
    healthy work and `classify_reap_reason` was filing it as a crash — while
    the watchdog that owns the run's clock believed it had up to 7200s.

    PER AGENT, because a fleet-wide number cannot be right for a fleet whose
    ceilings span two orders of magnitude: `benchmark-runner` declares
    timeout_seconds: 28800 while `curator` declares 600. A single global cutoff
    either reaps the benchmark mid-run or lets a wedged curator sit for hours.

    With no agent (or an unloadable one) it falls back to the most generous
    ceiling any run could hold. Erring long is deliberate: a late reap costs a
    stale row, an early one destroys live work and lies about why.
    """
    from robothor.engine.run_budget import effective_wallclock_ceiling
    from robothor.engine.watchdog_budgets import chain_for, max_wallclock_ceiling

    if agent_id:
        try:
            from robothor.engine.config import load_agent_config

            cfg = load_agent_config(agent_id, EngineConfig.from_env().manifest_dir)
            if cfg is not None:
                return (
                    effective_wallclock_ceiling(cfg.timeout_seconds, models=chain_for(cfg))
                    + REAP_GRACE_SECONDS
                )
        except Exception as e:  # noqa: BLE001 - an unreadable manifest must not stop the reap
            logger.debug("Reap cutoff fell back to the fleet ceiling for %s: %s", agent_id, e)
    return max_wallclock_ceiling() + REAP_GRACE_SECONDS


#: Live resume tasks, held so the event loop cannot collect one mid-run.
_RESUME_TASKS: set[asyncio.Task[Any]] = set()

#: Mirrors resume.MAX_RESUME_ATTEMPTS for the log line.
MAX_RESUME_ATTEMPTS_DISPLAY = 3


def _charge_resume_attempt(run_id: str) -> bool:
    """Charge one resume attempt. False means do not resume this run.

    Separated from the loop so the loop's real work — executing the run — can
    be tested without a database.
    """
    from robothor.db.connection import get_connection

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE agent_runs SET resume_attempts = COALESCE(resume_attempts, 0) + 1 "
                "WHERE id = %s",
                (run_id,),
            )
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001 - one uncharged run must not stop the rest
        logger.warning("Could not charge resume attempt for %s: %s", run_id, e)
        return False


async def _execute_resume(runner: Any, candidate: Any) -> None:
    """Actually continue the run. The step this function exists to perform.

    Same call shape as the operator-facing resume endpoint (health.py), so
    there is one way to resume a run rather than two that can drift.
    """
    from robothor.engine.models import TriggerType

    try:
        # TriggerType.EVENT, not MANUAL. MANUAL is INTERACTIVE: runner.py:583
        # gates it on a verified identity and, finding none, REJECTS the run —
        # silently, returning normally without creating a row or raising. The
        # operator-facing endpoint in health.py can use MANUAL because it has
        # an authenticated caller to pass; the daemon at startup does not.
        # Resume is a system action, so it takes a system trigger and inherits
        # the service identity like every other daemon-initiated run.
        await runner.execute(
            agent_id=candidate.agent_id,
            message="Resume from checkpoint — continue where you left off.",
            trigger_type=TriggerType.EVENT,
            trigger_detail=f"resume:{candidate.run_id}",
            resume_from_run_id=candidate.run_id,
        )
    except Exception:
        logger.exception("Resume of run %s failed", candidate.run_id)


def _resume_scan() -> list[ResumeCandidate]:
    """Every interrupted run the database knows about. [] when the scan fails.

    A named seam, not just tidiness: this is the ONE step in resume that needs
    a database, and while it was inline the only way for a test to reach the
    rest of the function was to patch an attribute that did not exist. A
    `monkeypatch.setattr(..., raising=False)` on a missing name patches
    nothing, so the resume test ran this query against whatever database the
    test host happened to have configured.
    """
    from robothor.engine.resume import RESUMABLE_STATUSES, ResumeCandidate

    try:
        from robothor.db.connection import get_connection
        from robothor.engine.checkpoint import CheckpointManager

        with get_connection() as conn:
            cur = conn.cursor()
            # Both interrupted states, not just `running`. A hard kill leaves
            # a row `running`; a GRACEFUL restart writes `cancelled` on the way
            # down -- and that is the restart that actually happens. On
            # 2026-08-27 a normal `systemctl restart` tombstoned five in-flight
            # runs two seconds before the new daemon scanned, three of them
            # holding checkpoints, and resume recovered none of them.
            cur.execute(
                "SELECT id, agent_id, COALESCE(resume_attempts, 0) FROM agent_runs "
                "WHERE status = ANY(%s) ORDER BY id",
                (sorted(RESUMABLE_STATUSES),),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("Resume scan failed: %s", _sanitize(e))
        return []

    return [
        ResumeCandidate(
            run_id=str(r[0]),
            agent_id=str(r[1] or ""),
            resume_attempts=int(r[2] or 0),
            has_checkpoint=bool(CheckpointManager.load_latest(str(r[0]))),
        )
        for r in rows
    ]


async def resume_interrupted_runs(runner: Any = None) -> int:
    """Resume runs a restart interrupted, before the reaper reaches them.

    Off unless ROBOTHOR_RESUME_IN_FLIGHT is set: this changes what a restart
    does to live work. Returns how many were started.

    Ordering matters — this must run BEFORE `_cleanup_stale_runs`, which
    marks every still-`running` row as timed out. Reaping first would destroy
    exactly the runs this exists to save.
    """
    from robothor.engine.resume import resume_batch, resume_enabled

    if not resume_enabled():
        return 0

    batch = resume_batch(_resume_scan())
    if not batch:
        return 0

    if runner is None:
        # Without a runner there is nothing to resume WITH. Returning 0 rather
        # than counting is the whole point: this function used to charge the
        # attempt, log "Resuming run ...", and return a count, having executed
        # nothing — so the daemon reported "resumed 3 interrupted agent runs"
        # while all three stayed `cancelled` forever.
        logger.warning("Resume skipped: no runner available to execute with")
        return 0

    started = 0
    for candidate in batch:
        # Charge the attempt BEFORE resuming: a run that dies during resume
        # must still have paid, or a crash loop resumes forever.
        if not _charge_resume_attempt(candidate.run_id):
            continue
        logger.info(
            "Resuming run %s (agent %s, attempt %d/%d)",
            candidate.run_id,
            _sanitize(candidate.agent_id),
            candidate.resume_attempts + 1,
            MAX_RESUME_ATTEMPTS_DISPLAY,
        )
        # Launched, not awaited: these are full agent runs and the daemon is
        # still coming up. Held in a module set because a bare create_task can
        # be garbage-collected mid-flight (this repo has been bitten before).
        task = asyncio.create_task(_execute_resume(runner, candidate))
        _RESUME_TASKS.add(task)
        task.add_done_callback(_RESUME_TASKS.discard)
        started += 1
    return started


def _set_daemon_start_ts() -> None:
    """Record the daemon's start time in-process and in env for reaper use."""
    global _DAEMON_START_TS
    from datetime import datetime

    ts = datetime.now(UTC).isoformat()
    _DAEMON_START_TS = ts
    os.environ["ROBOTHOR_DAEMON_START_TS"] = ts


def classify_reap_reason(
    run_id: str,
    started_at_iso: str,
    daemon_start_ts: str | None,
) -> tuple[str, str]:
    """Classify why a run was reaped. Returns (category, error_message).

    Categories:
        no_steps         — no agent_run_steps rows (likely crash during setup)
        post_llm_crash   — LLM was called; runner died after
        post_tool_crash  — last step was a tool_call / tool_result
        post_error_crash — last step was an 'error' step
        daemon_restart   — run started before current daemon boot
    """
    # daemon_restart wins if we know the daemon booted after this run started
    if daemon_start_ts and started_at_iso and started_at_iso < daemon_start_ts:
        return (
            "daemon_restart",
            f"Reaped by watchdog: likely cancelled by daemon restart at {daemon_start_ts}",
        )

    try:
        from robothor.engine.tracking import list_steps

        steps = list_steps(run_id)
    except Exception as e:
        logger.debug("classify_reap_reason: list_steps failed for %s: %s", run_id, e)
        steps = []

    if not steps:
        return (
            "no_steps",
            "Reaped by watchdog: no steps recorded (likely runner crash during setup)",
        )

    last = steps[-1]
    last_type = str(last.get("step_type") or "").lower()
    last_tool = last.get("tool_name")
    last_err = (last.get("error_message") or "").strip()

    if last_type == "error":
        msg = (
            f"Reaped by watchdog: runner crashed after error step (last_error={last_err[:160]})"
            if last_err
            else "Reaped by watchdog: runner crashed after error step"
        )
        return ("post_error_crash", msg)

    if last_type in ("tool_call", "tool_result"):
        return (
            "post_tool_crash",
            f"Reaped by watchdog: runner crashed after tool {last_tool or 'unknown'} "
            f"(last step_type={last_type})",
        )

    if last_type in ("llm_call", "llm_response"):
        return (
            "post_llm_crash",
            f"Reaped by watchdog: runner crashed after {last_type} (total steps={len(steps)})",
        )

    # Generic fallback — keep the category specific to what we saw
    return (
        "post_llm_crash"
        if any(s.get("step_type") in ("llm_call", "llm_response") for s in steps)
        else "no_steps",
        f"Reaped by watchdog: runner crashed after {last_type or 'unknown'} "
        f"(total steps={len(steps)})",
    )


def _cleanup_stale_workflow_runs() -> int:
    """Mark workflow_runs stuck 'running' for >2h as 'timeout'.

    Engine shutdown mid-run used to leave workflow_runs rows 'running'
    forever: retention excludes 'running' rows, so orphans were immortal
    (29 found in the 2026-08 diagnosis, oldest 171 days). The max workflow
    timeout is 900s, so anything 'running' for 2 hours is dead. Returns the
    number of rows reaped.
    """
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE workflow_runs SET status='timeout', "
                "completed_at=NOW(), "
                "duration_ms=EXTRACT(EPOCH FROM (NOW()-started_at))*1000, "
                "error_message='Reaped: engine restarted mid-run' "
                "WHERE status='running' AND started_at < NOW() - INTERVAL '2 hours'"
            )
            reaped = cur.rowcount or 0
            conn.commit()
            if reaped:
                logger.warning("Cleaned up %d stale workflow runs", reaped)
            return reaped
    except Exception as e:
        logger.warning("Stale workflow run cleanup failed: %s", e)
        return 0


def _cleanup_stale_runs() -> int:
    """Mark stale 'running' agent_runs as 'timeout' with per-run classification.

    Called on startup and periodically by the watchdog. Instead of applying a
    single hardcoded error_message to every reaped row, this now inspects the
    run's step history to produce a truthful diagnosis (see classify_reap_reason).
    Also reaps workflow_runs stuck 'running' >2h (engine restarts mid-run).

    Returns the number of runs cleaned up (agent + workflow).
    """
    wf_reaped = _cleanup_stale_workflow_runs()
    try:
        from robothor.db.connection import get_connection

        daemon_start_ts = _DAEMON_START_TS or os.environ.get("ROBOTHOR_DAEMON_START_TS")

        with get_connection() as conn:
            cur = conn.cursor()
            # Two tiers, because one number cannot serve both cases.
            #
            # ORPHAN: a run that predates this daemon's boot has no process
            # behind it, by definition. Reaped at once — strictly faster than
            # the old flat 30 minutes. (60s of slack so a run started during
            # boot is not mistaken for one that outlived the previous daemon.)
            #
            # LIVE: a run this daemon is still executing is reaped only past
            # the ceiling its own watchdog would enforce, plus grace. Anything
            # shorter means the reaper overrules the watchdog and calls healthy
            # work a crash — which is exactly what the flat 30 minutes did to
            # main's 33.5-minute average local-tier run.
            cur.execute(
                "SELECT id, agent_id, started_at "
                "FROM agent_runs "
                "WHERE status='running' AND ("
                "  (%(boot)s IS NOT NULL"
                "   AND started_at < %(boot)s::timestamptz - INTERVAL '60 seconds')"
                "  OR started_at < NOW() - make_interval(secs => %(cutoff)s)"
                ")",
                {"boot": daemon_start_ts, "cutoff": REAP_MIN_SCAN_SECONDS},
            )
            stale = cur.fetchall()
            if not stale:
                return wf_reaped

            for run_id, agent_id, started_at in stale:
                # The scan floor is deliberately cheap; this is the real gate.
                # An orphan (predating this boot) is reaped whatever its age —
                # nothing is executing it. A live run is reaped only past ITS
                # OWN agent's ceiling, so the reaper can never overrule the
                # watchdog and call healthy work a crash.
                if not _is_orphan(started_at, daemon_start_ts):
                    age = (datetime.now(UTC) - started_at).total_seconds() if started_at else 0.0
                    if age < stale_run_cutoff_seconds(str(agent_id or "")):
                        continue
                started_iso = started_at.isoformat() if started_at is not None else ""
                category, message = classify_reap_reason(str(run_id), started_iso, daemon_start_ts)
                cur.execute(
                    "UPDATE agent_runs SET status='timeout', "
                    "completed_at=NOW(), "
                    "duration_ms=EXTRACT(EPOCH FROM (NOW()-started_at))*1000, "
                    "error_message=%s, "
                    "reap_category=%s "
                    "WHERE id=%s AND status='running'",
                    (message, category, run_id),
                )
                logger.warning(
                    "Cleaned up stale run %s (agent=%s, category=%s)",
                    run_id,
                    agent_id,
                    category,
                )

            conn.commit()

            # Release dedup locks for cleaned-up agents
            from robothor.engine.dedup import release_sync

            for row in stale:
                release_sync(row[1])

            return len(stale) + wf_reaped
    except Exception as e:
        logger.warning("Stale run cleanup failed: %s", e)
        return wf_reaped


async def _start_federation(config: EngineConfig, runner: Any = None) -> Any:
    """Start federation NATS transport if connections exist.

    Returns the NATSManager (connected) or None. Backward-compatible no-op
    when no federation is configured. When ``runner`` is provided, also registers
    an inbound responder per active connection so peer federation_query/trigger
    calls are actually answered (not just sendable).
    """
    try:
        from robothor.federation.config import FederationConfig
        from robothor.federation.connections import load_connections
        from robothor.federation.models import ConnectionState
        from robothor.federation.nats import NATSManager

        # Resolve federation config: engine env vars → federation.yaml fallback
        fed_config = FederationConfig.from_env()
        instance_id = config.instance_id or fed_config.instance_id
        nats_url = config.nats_url or (fed_config.nats_url if fed_config.nats_enabled else "")

        if not instance_id:
            return None

        connections = load_connections()
        if not connections:
            logger.debug("Federation: no connections, skipping NATS")
            return None

        if not nats_url:
            # Configured-but-dead. This is NOT the quiet case above: rows exist,
            # so an operator believes this instance is federated. It has been in
            # exactly this state since 2026-03-09 with nothing louder than an
            # info line, which is why five months of total silence read as normal.
            await _alert_federation_dead(
                f"{len(connections)} federation connection(s) configured but no NATS "
                f"URL is set, so none can attach. This instance is NOT federated. "
                f"Set nats_url in federation.yaml or ROBOTHOR_NATS_URL."
            )
            return None

        # The transport owns every endpoint this instance holds. An instance
        # in the middle of an organisation is a child upward and a parent
        # downward, which one NATSManager cannot represent — so the manager is
        # now something the transport routes TO, not the thing held directly.
        from robothor.federation.transport import FederationTransport, set_transport

        # The engine's own credential for its own broker. Empty means an
        # unauthenticated server, which is only safe on loopback — and is what
        # this box ran until the leafnode listener was removed.
        hub_auth: dict[str, Any] = {}
        if fed_config.nats_user:
            hub_auth = {"user": fed_config.nats_user, "password": fed_config.nats_password}

        transport = FederationTransport(hub_url=nats_url, hub_options=hub_auth)

        nats_mgr = NATSManager(nats_url, **hub_auth)
        connected = await nats_mgr.connect()
        if connected:
            # Register the singleton HERE, not at the call site.
            # daemon.py:771 assigned the connected manager to a local variable
            # and nothing ever called set_nats_manager(), so
            # tools/handlers/federation.py's get_nats_manager() returned None
            # forever: a peer could query US (the responder below gets the
            # manager directly) but we could never query THEM. Outbound
            # federation was dead for five months and no test noticed, because
            # test_nats_request.py mocks the manager and calls the one function
            # production never reached. Registering inside the function that
            # creates it removes the possibility of forgetting.
            from robothor.federation.nats import set_nats_manager

            set_nats_manager(nats_mgr)
            set_transport(transport)
            logger.info(
                "Federation: NATS connected, %d connections loaded",
                len(connections),
            )

            # Attach every ACTIVE connection. Inbound peers are served on our
            # hub; outbound parents get their own dialled endpoint. A PENDING
            # row attaches nothing — activation is the handshake completing.
            from robothor.engine.federation_responder import make_command_handler
            from robothor.federation.connections import save_connection

            attached = 0
            pairing = 0
            for conn in connections:
                if conn.state not in (ConnectionState.ACTIVE, ConnectionState.PENDING):
                    continue

                # A PENDING inbound connection is admitted for pairing only:
                # the peer's hello is what activates it, so the parent has to
                # be listening first. The responder serves exactly one op in
                # that state.
                is_pairing = conn.state == ConnectionState.PENDING
                if is_pairing and conn.direction != "inbound":
                    continue  # we dial THEM to pair; that is `federation accept`

                handler = (
                    make_command_handler(
                        conn,
                        runner,
                        config=fed_config,
                        on_activate=save_connection,
                    )
                    if runner is not None
                    else None
                )
                if await transport.attach(conn, handler=handler, pending_ok=is_pairing):
                    if is_pairing:
                        pairing += 1
                    else:
                        attached += 1
                        # `last_seen_at` is the only honest answer to "is this
                        # link alive?", and it is written ONLY where the
                        # transport has actually been verified — here and in
                        # the heartbeat. Anything looser turns it into a record
                        # of the writer running.
                        from robothor.federation.connections import touch_last_seen

                        await asyncio.to_thread(touch_last_seen, conn.id)
                        if conn.direction == "inbound":
                            await nats_mgr.ensure_stream(conn.id)

            if pairing:
                logger.info(
                    "Federation: %d connection(s) listening for a pairing handshake", pairing
                )

            active = sum(1 for c in connections if c.state == ConnectionState.ACTIVE)
            if active and not attached:
                # Rows say active, the wire says nothing. This is the state the
                # box has been in since March, and it used to be a debug line.
                await _alert_federation_dead(
                    f"{active} federation connection(s) are marked ACTIVE but none "
                    f"could be attached to the transport. This instance is NOT "
                    f"federated despite what `federation status` reports."
                )
            elif attached < active:
                logger.warning("Federation: %d of %d active connections attached", attached, active)
        else:
            from robothor.federation.nats import set_nats_manager

            set_nats_manager(None)
            set_transport(None)
            await _alert_federation_dead(
                f"NATS connection to {nats_url} failed, so {len(connections)} "
                f"configured federation connection(s) are dead. This instance is "
                f"NOT federated."
            )
            return None

        return nats_mgr
    except Exception as e:
        # A crash here used to be a warning line. If connections are configured,
        # the operator needs to know federation is down, not find out months later.
        logger.exception("Federation startup failed (non-fatal)")
        await _alert_federation_dead(f"Federation startup raised {type(e).__name__}: {e}")
        return None


async def _federation_heartbeat() -> None:
    """Keep `last_seen_at` truthful, and page when a link drops.

    Runs unconditionally: an instance with no connections falls straight
    through, and an instance whose transport died is exactly the case that went
    unnoticed from 2026-03-09 to 2026-08-27.
    """
    from robothor.federation.heartbeat import heartbeat_loop

    try:
        await heartbeat_loop()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Federation heartbeat stopped")


async def _alert_federation_dead(detail: str) -> None:
    """Page when federation is configured but not carrying traffic.

    Deliberately NOT fired when there are no connections at all -- an instance
    that was never federated is not broken, and paging every single-box install
    would train the operator to mute the channel. The signal is the gap between
    "rows exist" and "transport attached".
    """
    try:
        from robothor.engine.alerts import alert

        await alert(
            "critical",
            "Federation is configured but not connected",
            f"{detail}\n\nCheck: robothor federation status",
        )
    except Exception:  # noqa: BLE001 - an alert must never break daemon startup
        logger.exception("could not alert on dead federation")


async def _maybe_run_alert_selftest() -> None:
    """Optional live probe of the alert path (env-gated). It must NOT page.

    ROBOTHOR_ALERT_SELFTEST=1 fires one alert at ``info`` shortly after
    startup, which ``alerts.alert()`` routes to an ``alert_digest`` row the
    operator agent surfaces on its next heartbeat. What that proves is that
    ``alert()`` runs on this box and reaches durable storage; the write is
    checked, not assumed.

    This probe has been wrong in both directions, and the level is where both
    mistakes live:

    * It first fired at ``info`` while its docstring claimed to verify the
      ``alert() -> send_fn(chat_id, text)`` arity end-to-end — the one thing
      ``info`` cannot do. An operator set the flag, saw no error, and
      concluded pages worked while a revoked bot token stayed invisible.
    * Raising it to ``critical`` made it honest and made it a pager. The
      engine restarts; the flag therefore paged CRITICAL on every start —
      52 pages in 7 days, not one of them an incident. A self-test that
      trains the operator to scroll past red costs more than the blind spot
      it closed.

    So the probe no longer claims to prove Telegram delivery, and no longer
    interrupts anyone to do it. Real delivery is proved by the paths that page
    for real: ``scripts/send_failure_alert.sh`` verifies its own send by HTTP
    status and spools what it could not deliver, and the liveness watchdog
    checks the sender's exit code rather than assuming it.

    Best-effort: never raises into the caller, but it does not fail quietly
    either — silence was the original defect.
    """
    if os.environ.get("ROBOTHOR_ALERT_SELFTEST") != "1":
        return
    try:
        from robothor.engine.alerts import alert

        recorded = await alert(
            "info",
            "Alert delivery self-test",
            "Engine startup self-test — this is a digest row, not a page. It "
            "confirms alert() runs and reaches the notification inbox. Unset "
            "ROBOTHOR_ALERT_SELFTEST to stop it.",
        )
    except Exception as e:
        logger.error("Alert delivery self-test RAISED: %s — the alert path is broken", e)
        return
    if recorded:
        logger.info("Alert delivery self-test: alert() wrote its alert_digest row.")
    else:
        logger.error(
            "Alert delivery self-test did NOT record an alert_digest row — alert() "
            "reached no durable store, so warning/info alerts are being lost."
        )


def _log_task_results(done: set[asyncio.Task[Any]]) -> bool:
    """Log the outcome of each finished top-level subsystem task.

    Mirrors task_registry.py's ``_on_done``: a task that ended cancelled
    must be skipped BEFORE calling ``.exception()`` — on a cancelled task
    that call raises ``CancelledError`` (a BaseException), which sails past
    ``except Exception`` in ``run()``'s top-level handler and kills the
    daemon outright. This is the containment layer for an orphaned stall
    watchdog cancelling the wrong task (Aug 5/9 crashes); the sleep-inside-
    try fixes on the curiosity/curator loops are the primary fix, this is
    the backstop for any other task that ends up cancelled.

    Returns:
        True when any finished task ended with an exception — i.e. this
        shutdown was triggered by a subsystem crash, not a clean stop.
        ``run()`` threads this into the process exit code so systemd's
        OnFailure pager fires (a crash that exits 0 crash-loops silently
        behind Restart=always forever).
    """
    failed = False
    for task in done:
        if task.cancelled():
            logger.error("Task %s was cancelled externally", task.get_name())
            continue
        if task.exception():
            logger.error("Task %s failed: %s", task.get_name(), task.exception())
            failed = True
        else:
            logger.info("Task %s completed", task.get_name())
    return failed


def _select_log_renderer() -> Any:
    """Pick the structlog renderer for this process.

    An explicit ``ROBOTHOR_LOG_FORMAT`` ("json" or "console") always wins.
    Without it, the dev ConsoleRenderer is used only when stdout is an
    interactive terminal; under systemd/journald the default is single-line
    JSON — the rich console renderer used to emit 224-line box-drawing
    tracebacks (with frame locals, including DSNs) per tool crash.
    """
    import structlog

    explicit = os.environ.get("ROBOTHOR_LOG_FORMAT", "").strip().lower()
    if explicit == "json":
        return structlog.processors.JSONRenderer()
    if explicit == "console":
        return structlog.dev.ConsoleRenderer()
    if sys.stdout.isatty():
        return structlog.dev.ConsoleRenderer()
    return structlog.processors.JSONRenderer()


#: The running scheduler, so the SIGHUP handler can re-register plugin jobs.
#: A module-level handle rather than a parameter because a signal handler
#: takes no arguments and this is the only state it needs.
_ACTIVE_SCHEDULER: Any = None


def _handle_plugin_reload_signal() -> int | None:
    """Re-discover plugins on SIGHUP, without dropping in-flight work.

    Installing a plugin previously required restarting the engine, which
    cancels every running agent — on this fleet that shows up as a batch of
    runs filed as timeouts and a silent operator. A reload only invalidates
    the caches; the four registries rebuild lazily on their next read, so
    nothing in flight is disturbed.

    Never raises. A reload that fails must leave the daemon running on the
    plugins it already had, not take it down.
    """
    try:
        gen = reload_plugins()
    except Exception as exc:  # noqa: BLE001 - a reload must never kill the daemon
        logger.warning("Plugin reload failed, keeping the current set: %s", exc)
        return None

    # Tools, schemas, guardrails, hooks and models rebuild lazily on next
    # use. Scheduled jobs cannot — nothing reads them again until they fire —
    # so the scheduler is told explicitly, or a job installed while the
    # engine is up would never run.
    scheduler = _ACTIVE_SCHEDULER
    if scheduler is not None:
        try:
            scheduler.register_plugin_jobs()
        except Exception as exc:  # noqa: BLE001 - keep the daemon up
            logger.warning("Plugin jobs could not be re-registered: %s", exc)
    return gen


def _install_plugin_reload_signal() -> bool:
    """Wire SIGHUP to a plugin reload. Returns whether it was installed.

    Extracted from ``main`` so a test can actually execute it. Left inline it
    ran only in a live daemon, and a NameError in these three lines would
    have surfaced as the engine failing to start — the suite was green with
    exactly that bug present.
    """
    with contextlib.suppress(Exception):  # not every platform has SIGHUP
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, _handle_plugin_reload_signal)
        return True
    return False


async def main() -> int:
    """Start all engine subsystems. Returns the process exit code."""
    # Reject unsafe production authentication before touching the database,
    # loading agents, or starting any background subsystem. The Engine verifies
    # Bridge-issued tokens but is not an SSO exchange authority, so it must not
    # require the Bridge's IdP credentials.
    validate_engine_auth_configuration()

    # Configure structured logging via structlog
    # Wraps stdlib logging so existing logging.getLogger() calls get structured output
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(processor=_select_log_renderer())

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    logger.info("Starting Genus OS Agent Engine...")
    # State this process's guardrail posture before anything runs. The flags
    # come from systemd Environment= lines on a single unit, so a second
    # daemon running this same code inherits none of them — which is exactly
    # what happened here for four days, silently.
    from robothor.engine.feature_flags import log_security_posture

    log_security_posture()

    # A tenant env conflict silently discards every default-tenant write in this
    # process (RLS refuses the row, the caller gets None). Say so at boot rather
    # than leaking one WARNING per refused write for months.
    from robothor.constants import tenant_env_conflict

    _tenant_conflict = tenant_env_conflict()
    if _tenant_conflict:
        logger.error("%s", _tenant_conflict)

    # Record daemon boot time before reaping so runs started before this boot
    # can be classified as 'daemon_restart' rather than 'post_llm_crash'.
    _set_daemon_start_ts()

    # Link the operator's CRM row to tenant_users.person_id (idempotent).
    # Driven by ~/.robothor/owner.yaml; no-op if not configured.
    try:
        from robothor.crm.dal import bootstrap_owner_person_links

        link_result = await asyncio.to_thread(bootstrap_owner_person_links)
        if link_result.get("linked"):
            logger.info(
                "Operator identity: linked tenant=%s → person_id=%s%s",
                link_result.get("tenant_id"),
                link_result.get("person_id"),
                " (created new person)" if link_result.get("created_person") else "",
            )
    except Exception:
        logger.exception("bootstrap_owner_person_links failed (non-fatal)")

    # Load config
    config = EngineConfig.from_env()
    logger.info("Tenant: %s", config.tenant_id)
    logger.info("Workspace: %s", config.workspace)
    logger.info("Health port: %d", config.port)
    logger.info("Telegram bot: %s", "configured" if config.bot_token else "disabled")

    # Create subsystems
    runner = AgentRunner(config)

    # Resume BEFORE reaping: `_cleanup_stale_runs` marks every interrupted row
    # terminal, which would destroy exactly what resume recovers. This block
    # sits after the runner is constructed because resume needs something to
    # resume WITH — when it ran earlier it had no runner, and quietly counted
    # runs it never executed.
    try:
        resumed = await resume_interrupted_runs(runner)
        if resumed:
            logger.info("Startup: resumed %d interrupted agent runs", resumed)
    except Exception as e:
        logger.warning("Startup resume failed, continuing to reap: %s", _sanitize(e))

    cleaned = await asyncio.to_thread(_cleanup_stale_runs)
    if cleaned:
        logger.info("Startup: cleaned %d stale agent runs", cleaned)

    # Initialize fleet pool for admission control
    from robothor.engine.pool import init_fleet_pool

    init_fleet_pool(
        max_concurrent=config.max_concurrent_agents,
        hourly_cost_cap_usd=config.hourly_cost_cap_usd,
    )
    logger.info(
        "Fleet pool: max_concurrent=%d, hourly_cost_cap=$%.2f",
        config.max_concurrent_agents,
        config.hourly_cost_cap_usd,
    )

    # Initialize inter-agent messaging + teams so the send_agent_message /
    # receive_agent_messages / create_team / team_scratchpad_* tools work
    # (their handlers no-op with "not initialized" until these are called).
    from robothor.engine.messaging import init_messenger
    from robothor.engine.teams import init_team_manager

    init_messenger()
    init_team_manager()
    logger.info("Inter-agent messaging + teams initialized")

    # HA: scheduler leadership elector (no-op / always-leader when HA off).
    from robothor.engine.leader import LeaderElector, ha_leader_enabled, set_elector

    _elector = LeaderElector()
    set_elector(_elector)
    if ha_leader_enabled():
        logger.info("HA leader election enabled")

    # Initialize lifecycle hook registry
    from robothor.engine.hook_registry import (
        init_hook_registry,
        load_global_hooks,
        load_hooks_from_manifest,
    )

    hook_registry = init_hook_registry()

    # Register channel-bus surface handler so POST_DELIVERY dual-writes fleet
    # outputs into main's session for supervisor visibility. Must be registered
    # before load_global_hooks so the YAML entry can bind to it by name.
    from robothor.engine.channel_bus import on_post_delivery as _channel_bus_surface

    hook_registry.register_python_handler("channel_bus.surface", _channel_bus_surface)

    # Channel-bus main busy-state hooks: AGENT_START/AGENT_END on the main
    # agent flip a flag in the WakeDebouncer so wakes deferred during a
    # concurrent interactive turn can re-fire cleanly when main goes idle.
    async def _channel_bus_on_main_start(ctx: Any) -> Any:
        from robothor.engine.channel_bus import get_debouncer
        from robothor.engine.hook_registry import HookResult

        if ctx.agent_id == "main":
            deb = get_debouncer()
            if deb:
                deb.mark_main_started()
        return HookResult()

    async def _channel_bus_on_main_end(ctx: Any) -> Any:
        from robothor.engine.channel_bus import get_debouncer
        from robothor.engine.hook_registry import HookResult

        if ctx.agent_id == "main":
            deb = get_debouncer()
            if deb:
                await deb.mark_main_finished()
        return HookResult()

    hook_registry.register_python_handler("channel_bus.main_started", _channel_bus_on_main_start)
    hook_registry.register_python_handler("channel_bus.main_finished", _channel_bus_on_main_end)

    # Plugin-contributed lifecycle handlers, AFTER the engine's own so that an
    # installed package cannot claim a channel_bus.* name. Registering these
    # was the last of #411's four declared entry-point groups to have no
    # consumer at all.
    from robothor.engine.hook_registry import register_plugin_hooks

    register_plugin_hooks(hook_registry)

    global_hooks = load_global_hooks(config.workspace / "docs" / "hooks")
    if global_hooks:
        hook_registry.register_many(global_hooks)
        logger.info("Loaded %d global lifecycle hooks", len(global_hooks))

    # Load per-agent lifecycle hooks from manifests
    from robothor.engine.config import load_all_manifests

    agent_hook_count = 0
    for manifest in load_all_manifests(config.manifest_dir):
        agent_id = manifest.get("id", "")
        agent_hooks = load_hooks_from_manifest(manifest, agent_id)
        if agent_hooks:
            hook_registry.register_many(agent_hooks)
            agent_hook_count += len(agent_hooks)
    if agent_hook_count:
        logger.info("Loaded %d agent lifecycle hooks", agent_hook_count)

    # Register buddy lifecycle hooks
    from robothor.engine.buddy_hooks import register_buddy_hooks

    register_buddy_hooks(hook_registry)

    # Register runner for sub-agent spawning
    from robothor.engine.tools import set_runner

    set_runner(runner, config)

    workflow_engine = WorkflowEngine(config, runner)
    wf_count = workflow_engine.load_workflows(config.workflow_dir)
    logger.info("Loaded %d workflows", wf_count)

    bot = TelegramBot(config, runner) if config.bot_token else None
    if bot is None:
        logger.warning(
            "ROBOTHOR_TELEGRAM_BOT_TOKEN is empty — Telegram delivery disabled. "
            "Engine API, scheduler, and hooks will still run."
        )
    else:
        # Wire human-approval escalations to the operator's Telegram. Without
        # this, get_permission_manager() returns None and every human_approval
        # escalation auto-approves (see runner fail-closed branch).
        from robothor.engine.permission_escalation import init_permission_manager

        init_permission_manager(bot, config.default_chat_id)
        logger.info("Permission escalation manager wired to Telegram")
    scheduler = CronScheduler(config, runner, workflow_engine=workflow_engine)
    global _ACTIVE_SCHEDULER
    _ACTIVE_SCHEDULER = scheduler
    hooks = EventHooks(config, runner, workflow_engine=workflow_engine)

    # Channel-bus debouncer: submits fleet surfaces, fires CHANNEL_EVENT wake
    # on main after the debounce window. Uses main's YAML config for timing.
    try:
        from robothor.engine.channel_bus import WakeDebouncer, set_debouncer
        from robothor.engine.config import load_agent_config as _load_main_cfg

        _main_cfg = _load_main_cfg("main", config.manifest_dir)
        cb_cfg = _main_cfg.channel_bus if _main_cfg else None
        if cb_cfg is not None:
            _debouncer = WakeDebouncer(
                trigger_fn=scheduler.trigger_channel_event,
                debounce_seconds=cb_cfg.wake_debounce_seconds,
                cooldown_seconds=cb_cfg.main_wake_cooldown_seconds,
                rate_limit_per_hour=cb_cfg.per_agent_rate_limit_per_hour,
                enabled=cb_cfg.wake_on_surface,
            )
            set_debouncer(_debouncer)
            logger.info(
                "Channel-bus wake: enabled=%s debounce=%ds cooldown=%ds limit=%d/hr",
                cb_cfg.wake_on_surface,
                cb_cfg.wake_debounce_seconds,
                cb_cfg.main_wake_cooldown_seconds,
                cb_cfg.per_agent_rate_limit_per_hour,
            )
    except Exception as e:
        logger.warning("Channel-bus debouncer init failed (non-fatal): %s", e)

    # Federation — start NATS if connections exist (no-op otherwise)
    nats_mgr = await _start_federation(config, runner=runner)

    # Start all subsystems concurrently
    tasks = [
        asyncio.create_task(scheduler.start(), name="scheduler"),
        asyncio.create_task(hooks.start(), name="hooks"),
        asyncio.create_task(
            serve_health(config, runner=runner, workflow_engine=workflow_engine),
            name="health",
        ),
        asyncio.create_task(_watchdog(config, scheduler, workflow_engine), name="watchdog"),
        asyncio.create_task(_autodream_loop(), name="autodream"),
        asyncio.create_task(_federation_heartbeat(), name="federation-heartbeat"),
        asyncio.create_task(_curiosity_density_loop(scheduler), name="curiosity-density"),
        asyncio.create_task(_curator_loop(scheduler), name="curator"),
        asyncio.create_task(_extension_watcher_loop(), name="extensions"),
        asyncio.create_task(_elector.run(), name="leader"),
    ]
    if bot is not None:
        tasks.insert(0, asyncio.create_task(bot.start_polling(), name="telegram"))

    # Slack channel (Socket Mode) — env-gated; start() self-gates on the tokens.
    slack_bot = None
    if os.environ.get("ROBOTHOR_SLACK_BOT_TOKEN") and os.environ.get("ROBOTHOR_SLACK_APP_TOKEN"):
        from robothor.engine.slack import SlackBot

        slack_bot = SlackBot(runner, config)
        tasks.append(asyncio.create_task(slack_bot.start(), name="slack"))
        logger.info("Slack channel enabled (Socket Mode)")

    logger.info("All subsystems started")
    _sd_notify("READY=1")

    # Startup announcement (best-effort)
    try:
        from robothor.engine.config import load_all_manifests, manifest_to_agent_config

        manifests = load_all_manifests(config.manifest_dir)
        scheduled = sum(1 for m in manifests if manifest_to_agent_config(m).cron_expr)
        from robothor.engine.delivery import get_telegram_sender

        sender = get_telegram_sender()
        if sender and config.default_chat_id:
            await sender(
                config.default_chat_id,
                f"*Engine Online*\n\n"
                f"{scheduled} scheduled agents loaded.\n"
                f"Port {config.port} | Tenant {config.tenant_id}",
            )
    except Exception as e:
        logger.debug("Startup announcement failed: %s", e)

    # Alert delivery self-test (env-gated, best-effort) — see docstring.
    await _maybe_run_alert_selftest()

    _install_plugin_reload_signal()
    # Tell agents that ask what is in their workspace. Opt-in per manifest —
    # an operator's home directory is not a useful listing — but registered
    # unconditionally so the manifest flag is all that is needed.
    with contextlib.suppress(Exception):
        from robothor.engine.workspace_inventory import register as _register_inventory

        _register_inventory()

    # Wait for any task to complete (aiogram handles SIGTERM and stops polling,
    # which completes the telegram task — that's our shutdown trigger)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Log what finished — and remember whether this shutdown is a subsystem
    # crash (non-zero exit → OnFailure pages) or a clean stop (exit 0).
    subsystem_crashed = _log_task_results(done)

    logger.info("Shutting down subsystems...")

    # A stale singleton after shutdown would let a tool call reach a manager
    # whose connection is gone, and report "transport not connected" as though
    # federation were merely unconfigured.
    try:
        from robothor.federation.nats import set_nats_manager
        from robothor.federation.transport import get_transport, set_transport

        set_nats_manager(None)
        live = get_transport()
        set_transport(None)
        if live is not None:
            await live.close()
    except Exception as e:  # noqa: BLE001 - shutdown must not raise
        logger.debug("Error closing live-steering channel during shutdown: %s", e)

    # Shutdown announcement (best-effort)
    try:
        from robothor.engine.dedup import running_agents
        from robothor.engine.delivery import get_telegram_sender

        active = running_agents()
        sender = get_telegram_sender()
        if sender and config.default_chat_id:
            active_str = ", ".join(active) if active else "none"
            await sender(
                config.default_chat_id,
                f"*Engine Shutting Down*\n\nActive agents: {active_str}",
            )
    except Exception as e:
        logger.debug("Shutdown announcement failed: %s", e)

    # Disconnect federation NATS (if connected)
    if nats_mgr is not None:
        try:
            await nats_mgr.disconnect()
            logger.info("Federation: NATS disconnected")
        except Exception as e:
            logger.debug("Federation NATS disconnect failed: %s", e)

    # Drain tracked background tasks before stopping subsystems
    from robothor.engine.task_registry import get_task_registry

    await get_task_registry().drain()

    await scheduler.stop()
    await hooks.stop()
    if bot is not None:
        await bot.stop()
    if slack_bot is not None:
        try:
            await slack_bot.stop()
        except Exception as e:
            logger.debug("Slack bot stop failed: %s", e)

    # Release the leadership lease explicitly before cancelling its task —
    # otherwise the lease sits until its TTL expires and the whole fleet is
    # leaderless (no cron/heartbeat) for up to that window.
    try:
        await _elector.stop()
    except Exception as e:
        logger.debug("Leader elector stop failed: %s", e)

    # Cancel remaining tasks
    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)
    logger.info("Engine stopped")
    return 1 if subsystem_crashed else 0


def _record_watchdog_event(event_type: str, detail: str) -> None:
    """Append a watchdog event to the watchdog_log memory block."""
    try:
        from datetime import UTC, datetime

        from robothor.memory.blocks import read_block, write_block

        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"[{ts}] {event_type}: {detail}"
        existing = read_block("watchdog_log")
        content = existing.get("content", "") if "error" not in existing else ""
        lines = [ln for ln in content.strip().splitlines() if ln] if content else []
        lines.append(entry)
        lines = lines[-50:]
        write_block("watchdog_log", "\n".join(lines))
    except Exception:
        logger.debug("Watchdog event recording failed", exc_info=True)


# Daily maintenance (chat-session TTL + data retention) fires on wall-clock,
# not uptime: the old `tick_count % 2880` gate required 24h of *continuous*
# uptime, which a daemon restarting more than once a day never reached, so
# retention silently starved.
_DAILY_MAINTENANCE_INTERVAL_SECONDS = 24 * 3600

# Sustained-outage detectors read 7–14 day windows; running them more often
# than this just re-derives the same verdict.
_OUTAGE_DETECTOR_INTERVAL_SECONDS = 2 * 3600

# Matches the entry format written by _record_watchdog_event, e.g.
# a line of the form ``[<YYYY-MM-DD HH:MM> UTC] <event_type>: <detail>``.
_WATCHDOG_EVENT_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\] ([a-z_]+):")


def _read_last_watchdog_event_ts(*event_types: str) -> float | None:
    """Newest UNIX timestamp among watchdog_log entries of the given types.

    Reads the same memory block _record_watchdog_event() appends to, so the
    last-run marker survives daemon restarts. Returns None when the block is
    missing, unreadable, or holds no matching entry.
    """
    from datetime import UTC, datetime

    try:
        from robothor.memory.blocks import read_block

        existing = read_block("watchdog_log")
        content = existing.get("content", "") if "error" not in existing else ""
    except Exception:
        logger.debug("Reading watchdog_log block failed", exc_info=True)
        return None

    latest: float | None = None
    for line in (content or "").splitlines():
        match = _WATCHDOG_EVENT_RE.match(line.strip())
        if not match or match.group(2) not in event_types:
            continue
        try:
            ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _daily_maintenance_due(now: float, last_run: float | None) -> bool:
    """True when daily maintenance should fire: never run, or >=24h ago."""
    return last_run is None or (now - last_run) >= _DAILY_MAINTENANCE_INTERVAL_SECONDS


async def _watchdog(
    config: EngineConfig,
    scheduler: CronScheduler,
    workflow_engine: WorkflowEngine | None = None,
) -> None:
    """Subsystem watchdog — pings PostgreSQL and Redis every 30s, notifies systemd, cleans stale sessions daily."""
    global _autodream_stale_alerted  # noqa: PLW0603

    pg_failures = 0
    redis_failures = 0
    tick_count = 0
    # 0.0 = "never run this process", so the first eligible tick after boot
    # runs the sustained-outage detectors instead of waiting out the interval.
    outage_detectors_last = 0.0

    # Wall-clock gate for daily maintenance — persisted via the watchdog_log
    # block so restarts don't reset the clock. Legacy retention_* event names
    # are recognized so the first post-upgrade boot doesn't re-fire early.
    loop = asyncio.get_running_loop()
    daily_maintenance_last: float | None = await loop.run_in_executor(
        None,
        lambda: _read_last_watchdog_event_ts(
            "daily_maintenance", "retention_cleanup", "retention_timeout"
        ),
    )

    while True:
        await asyncio.sleep(30)
        tick_count += 1
        _sd_notify("WATCHDOG=1")

        # Ping PostgreSQL (with timeout to prevent event loop blocking)
        try:
            loop = asyncio.get_running_loop()

            def _pg_ping() -> None:
                from robothor.db.connection import get_connection

                with get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")

            await asyncio.wait_for(loop.run_in_executor(None, _pg_ping), timeout=10.0)
            pg_failures = 0
        except Exception as e:
            pg_failures += 1
            logger.warning("Watchdog: PostgreSQL ping failed (%d): %s", pg_failures, e)
            if pg_failures in (1, 3, 10) or (pg_failures > 10 and pg_failures % 100 == 0):
                _record_watchdog_event("pg_failure", f"consecutive={pg_failures}: {e}")

        # Ping Redis (with timeout to prevent event loop blocking)
        try:
            loop = asyncio.get_running_loop()

            def _redis_ping() -> None:
                import redis as _redis

                from robothor.config import get_config

                cfg = get_config()
                r = _redis.Redis(
                    host=cfg.redis.host,
                    port=cfg.redis.port,
                    db=cfg.redis.db,
                    password=cfg.redis.password or None,
                )
                r.ping()
                r.close()

            await asyncio.wait_for(loop.run_in_executor(None, _redis_ping), timeout=10.0)
            redis_failures = 0
        except Exception as e:
            redis_failures += 1
            logger.warning("Watchdog: Redis ping failed (%d): %s", redis_failures, e)
            if redis_failures in (1, 3, 10) or (redis_failures > 10 and redis_failures % 100 == 0):
                _record_watchdog_event("redis_failure", f"consecutive={redis_failures}: {e}")

        # Schedule reconciliation (every 10 ticks = 5 minutes)
        if tick_count % 10 == 0:
            try:
                # reconcile() pages when a manifest cannot be read, and refuses
                # to prune anything from an incomplete scan.
                pruned = await scheduler.reconcile()
                if pruned:
                    logger.info("Watchdog: reconciled schedules, pruned: %s", pruned)
            except Exception as e:
                logger.warning("Watchdog: schedule reconciliation failed: %s", e)

        # Approval driver (every 2 ticks = 1 minute). A decided approval that
        # nothing acts on leaves the run suspended forever — the "built,
        # merged, and not running" failure this platform keeps finding. Two
        # indexed scans; on a box with no pending approvals it is a no-op.
        if tick_count % 2 == 0 and workflow_engine is not None:
            try:
                await workflow_engine.drive_approvals()
            except Exception as e:
                logger.warning("Watchdog: approval driver failed: %s", e)

        # Zombie run reaper (every 40 ticks = 20 minutes)
        if tick_count % 40 == 0:
            try:
                loop = asyncio.get_running_loop()
                reaped = await loop.run_in_executor(None, _cleanup_stale_runs)
                if reaped:
                    logger.warning("Watchdog: reaped %d zombie agent runs", reaped)
            except Exception as e:
                logger.warning("Watchdog: zombie reaper failed: %s", e)

        # Failure-mode detectors — read-only, alerts only, never kills runs.
        # Disabled via ROBOTHOR_DETECTORS_ENABLED=0.
        # Runaway burn: every 4 ticks = 2 minutes
        if tick_count % 4 == 0:
            try:
                from robothor.engine.detectors import runaway_burn_detector

                fired = await runaway_burn_detector()
                if fired:
                    logger.info("Detectors: %d runaway-burn alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: runaway_burn check failed: %s", e)

        # Repeat errors + tool degradation: every 10 ticks = 5 minutes
        if tick_count % 10 == 0:
            try:
                from robothor.engine.detectors import (
                    repeat_error_detector,
                    tool_degradation_detector,
                )

                fired = await repeat_error_detector(tenant_id=config.tenant_id)
                if fired:
                    logger.info("Detectors: %d repeat-error alerts fired", fired)
                fired = await tool_degradation_detector()
                if fired:
                    logger.info("Detectors: %d tool-degradation alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: pattern checks failed: %s", e)

        # Zombie runner: every 20 ticks = 10 minutes
        if tick_count % 20 == 0:
            try:
                from robothor.engine.detectors import zombie_runner_detector

                fired = await zombie_runner_detector()
                if fired:
                    logger.info("Detectors: %d zombie-runner alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: zombie_runner check failed: %s", e)

        # Workflow health (stuck runs + failure streaks): every 20 ticks = 10 min
        if tick_count % 20 == 0:
            try:
                from robothor.engine.detectors import (
                    stuck_workflow_detector,
                    workflow_failure_streak_detector,
                )

                fired = await stuck_workflow_detector()
                if fired:
                    logger.info("Detectors: %d stuck-workflow alerts fired", fired)
                fired = await workflow_failure_streak_detector()
                if fired:
                    logger.info("Detectors: %d workflow-failure-streak alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: workflow health checks failed: %s", e)

        # Sustained outages (dead tool dependency, primary model unreached):
        # every 2h on wall-clock, first pass ~10 min after boot. These read
        # multi-day windows, so a faster cadence would only re-derive the same
        # answer (their alerts dedup for 24h) — but a plain `tick_count % 240`
        # gate would need two hours of *continuous* uptime, so a daemon
        # restarting more often than that would never run them at all. That is
        # the starvation the daily-maintenance gate above was rewritten to fix.
        if (
            tick_count % 20 == 0
            and (time.time() - outage_detectors_last) >= _OUTAGE_DETECTOR_INTERVAL_SECONDS
        ):
            outage_detectors_last = time.time()
            try:
                from robothor.engine.detectors import (
                    primary_model_unreached_detector,
                    tool_outage_detector,
                )

                fired = await tool_outage_detector()
                if fired:
                    logger.info("Detectors: %d tool-outage alerts fired", fired)
                fired = await primary_model_unreached_detector(tenant_id=config.tenant_id)
                if fired:
                    logger.info("Detectors: %d primary-model-unreached alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: outage checks failed: %s", e)

        # Daily maintenance: chat-session TTL cleanup + data retention.
        # Wall-clock gated (>=24h since the persisted last run, checked every
        # 10 ticks = 5 min) so daemon restarts can never starve it — the old
        # `% 2880` gate needed 24h of continuous uptime.
        if tick_count % 10 == 0 and _daily_maintenance_due(time.time(), daily_maintenance_last):
            daily_maintenance_last = time.time()
            # Recorded up front (and unconditionally) so a failed or empty
            # sweep still advances the persisted clock instead of re-firing
            # every 5 minutes.
            _record_watchdog_event("daily_maintenance", "chat TTL + retention sweep started")

            try:
                from robothor.engine.chat_store import cleanup_stale_sessions

                loop = asyncio.get_running_loop()
                deleted = await loop.run_in_executor(None, cleanup_stale_sessions)
                if deleted:
                    logger.info("Watchdog: cleaned up %d stale chat sessions", deleted)
            except Exception as e:
                logger.warning("Watchdog: chat session cleanup failed: %s", e)

            try:
                from robothor.engine.retention import run_retention_cleanup

                loop = asyncio.get_running_loop()
                results = await asyncio.wait_for(
                    loop.run_in_executor(None, run_retention_cleanup),
                    timeout=300.0,
                )
                total = sum(v for v in results.values() if v > 0)
                if total > 0:
                    logger.info("Watchdog: retention cleanup deleted %d rows: %s", total, results)
                    _record_watchdog_event("retention_cleanup", f"deleted {total} rows")
            except TimeoutError:
                logger.warning("Watchdog: retention cleanup timed out after 300s")
                _record_watchdog_event("retention_timeout", "cleanup exceeded 300s")
            except Exception as e:
                logger.warning("Watchdog: retention cleanup failed: %s", e)

        # Chat-message embedding backfill (every 60 ticks = 30 min).
        # Sweeps rows the fire-and-forget embed task missed (process restart)
        # and everything the sync scheduler save path never embeds at all.
        if tick_count % 60 == 0:
            try:
                from robothor.engine.chat_store import backfill_chat_embeddings

                embedded = await backfill_chat_embeddings()
                if embedded:
                    logger.info("Watchdog: backfilled embeddings for %d chat messages", embedded)
            except Exception as e:
                logger.warning("Watchdog: chat embedding backfill failed: %s", e)

        # autoDream staleness check (every 20 ticks = 10 min)
        if tick_count % 20 == 0 and tick_count > 20:
            try:
                from robothor.engine.autodream import COOLDOWN_SECONDS

                last_run, source = _resolve_last_run()
                staleness = (time.time() - last_run) if last_run is not None else None
                decision = _autodream_staleness_decision(
                    staleness,
                    _autodream_stale_alerted,
                    _AUTODREAM_MAX_DEFER_SECONDS,
                    COOLDOWN_SECONDS,
                )
                if decision["reset"]:
                    _autodream_stale_alerted = False
                if decision["warn"] and staleness is not None:
                    logger.warning(
                        "Watchdog: autoDream stale (last run %.0f min ago, source=%s)",
                        staleness / 60,
                        source,
                    )
                if decision["alert"] and staleness is not None:
                    from robothor.engine.delivery import get_telegram_sender

                    sender = get_telegram_sender()
                    if sender and config.default_chat_id:
                        hours = int(staleness / 3600)
                        await sender(
                            config.default_chat_id,
                            f"*Watchdog Alert*\n\nautoDream has not run for {hours}h "
                            f"(source: {source}). Memory consolidation may be stalled.",
                        )
                        # Latch only after a delivered page so a transient
                        # sender outage doesn't suppress the alert.
                        _autodream_stale_alerted = True
            except Exception as e:
                logger.debug("Watchdog: autoDream staleness check failed: %s", e)

        # Alert after 3 consecutive PG failures
        if pg_failures == 3:
            try:
                from robothor.engine.delivery import get_telegram_sender

                sender = get_telegram_sender()
                if sender and config.default_chat_id:
                    await sender(
                        config.default_chat_id,
                        "*Watchdog Alert*\n\nPostgreSQL unreachable for 3 consecutive checks.",
                    )
            except Exception:
                # A page that fails silently is worse than no page: the database
                # outage and the failed alert then look identical from outside —
                # nothing. The journal is monitored, so ERROR here is the last
                # place this can still be seen.
                logger.error(
                    "Watchdog could not deliver the PostgreSQL-unreachable alert; "
                    "the outage is UNREPORTED",
                    exc_info=True,
                )


_CURIOSITY_COOLDOWN_SECONDS = 6 * 3600  # 6h minimum between reactive spawns
_CURIOSITY_CHECK_INTERVAL = 30 * 60  # check density every 30 min
_curiosity_last_spawn_ts: float = 0.0


async def _curiosity_density_loop(scheduler: Any) -> None:
    """Reactive curiosity engine spawner.

    Every 30 minutes, polls gap_analysis metrics. If knowledge density is low
    (orphans / thin clusters / uncertainty signals cross thresholds) AND the
    reactive cooldown has elapsed, spawn the curiosity-engine agent off-cycle
    so it can fill gaps without waiting for the Sunday cron.

    The cron-scheduled Sunday run remains as a floor.
    """
    global _curiosity_last_spawn_ts  # noqa: PLW0603

    from robothor.engine.dedup import running_agents
    from robothor.memory.gap_analysis import get_memory_density_metrics

    # Initial wait so we don't spawn immediately after boot
    await asyncio.sleep(300)

    while True:
        try:
            # Sleep inside the try — a cancel landing here (e.g. from an
            # orphaned stall watchdog monitoring the wrong task) must be
            # caught by except asyncio.CancelledError below, not propagate
            # out of the coroutine and mark this task itself as cancelled.
            await asyncio.sleep(_CURIOSITY_CHECK_INTERVAL)
            now = time.time()
            if now - _curiosity_last_spawn_ts < _CURIOSITY_COOLDOWN_SECONDS:
                continue
            if "curiosity-engine" in running_agents():
                continue

            metrics = await get_memory_density_metrics()
            if not metrics.get("should_spawn"):
                continue

            logger.info(
                "curiosity-density: reactive spawn triggered "
                "(orphans=%d thin=%d uncertainty=%d low_conf=%d)",
                metrics["orphan_count"],
                metrics["thin_cluster_count"],
                metrics["uncertainty_count"],
                metrics["low_confidence_count"],
            )
            _curiosity_last_spawn_ts = now
            await scheduler._run_agent("curiosity-engine")  # noqa: SLF001
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("curiosity-density loop error: %s", e)


_CURATOR_CHECK_INTERVAL = 6 * 3600  # re-evaluate cadence every 6h


async def _curator_loop(scheduler: Any) -> None:
    """Skill-library maintenance (Phase 3 / Rip 5).

    Tier 1 ALWAYS (no flag, no LLM): apply_skill_lifecycle() reports time-derived
    stale/archived states (pure-derived, never persisted) — anti-bloat telemetry.
    Tier 2 OPT-IN (curator_enabled(), default OFF): destructive LLM consolidation.
    Cadence keyed to should_run_curator (default 7d); only acts when engine idle.
    """
    from datetime import UTC, datetime

    from robothor.engine.curator import (
        load_curator_last_pass,
        should_run_curator,
        spawn_curator,
        store_curator_last_pass,
    )
    from robothor.engine.dedup import running_agents
    from robothor.engine.feature_flags import curator_enabled
    from robothor.engine.skills import apply_skill_lifecycle

    await asyncio.sleep(600)  # stagger past boot

    while True:
        try:
            # Sleep inside the try — a cancel landing here (e.g. from an
            # orphaned stall watchdog monitoring the wrong task) must be
            # caught by except asyncio.CancelledError below, not propagate
            # out of the coroutine and mark this task itself as cancelled.
            await asyncio.sleep(_CURATOR_CHECK_INTERVAL)
            if not should_run_curator(load_curator_last_pass()):
                continue
            if running_agents():
                logger.debug("curator: engine busy, deferring pass")
                continue

            loop = asyncio.get_running_loop()
            lifecycle = await loop.run_in_executor(None, apply_skill_lifecycle)
            if any(lifecycle.values()):
                logger.info("curator: lifecycle report %s", lifecycle)

            if curator_enabled():
                if running_agents():
                    logger.info("curator: busy at LLM gate, deferring consolidation")
                else:
                    await spawn_curator(scheduler)
            else:
                logger.debug("curator: RIP_5 off — lifecycle only, no LLM pass")

            await loop.run_in_executor(None, store_curator_last_pass, datetime.now(UTC))
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("curator loop error: %s", e)


async def _extension_watcher_loop() -> None:
    """Hot-reload adapter YAML changes without an engine restart (ExtensionWatcher)."""
    try:
        from robothor.engine.extensions import ExtensionWatcher

        await ExtensionWatcher().watch()
    except Exception as e:
        logger.debug("Extension watcher exited: %s", e)


# Maximum seconds to defer autoDream when agents are continuously busy.
# After this ceiling the loop fires anyway (post_stall mode) to prevent
# memory consolidation from being starved on active deployments.
_AUTODREAM_MAX_DEFER_SECONDS = int(os.environ.get("AUTODREAM_MAX_DEFER_SECONDS", str(4 * 3600)))

# Extra back-off when a forced post_stall run is skipped because a concurrent
# run already holds the lock — avoids a 60s busy-loop while that run finishes.
_AUTODREAM_FORCE_BACKOFF_SECONDS = int(os.environ.get("AUTODREAM_FORCE_BACKOFF_SECONDS", "600"))

# In-process state for the autoDream loop/watchdog. Both run in the same event
# loop (no threads), and neither awaits between reading and writing these, so
# plain module globals are safe.
_autodream_defer_started_at: float | None = None  # start of the current continuous-busy streak
_autodream_stale_alerted: bool = False  # latch so the staleness page fires once per episode


def _resolve_last_run() -> tuple[float | None, str]:
    """Resolve the last-run timestamp and the tier that served it.

    Single source of truth shared by `_watchdog` and `_autodream_loop`: reads
    the validated timestamp from autodream, and when none is usable anchors to
    daemon boot time so a fresh / Redis-down daemon still has a clock. The
    source faithfully reflects the tier ("redis" | "file" | "daemon boot" |
    "unknown"), so the watchdog alert can no longer mislabel a filesystem hit.
    """
    from robothor.engine.autodream import _get_last_run_ts_with_source

    ts, source = _get_last_run_ts_with_source()
    if ts is not None:
        return ts, source
    if _DAEMON_START_TS:
        try:
            from datetime import datetime as _dt

            return _dt.fromisoformat(_DAEMON_START_TS).timestamp(), "daemon boot"
        except Exception:
            return None, "unknown"
    return None, "unknown"


def _autodream_staleness_decision(
    staleness: float | None,
    already_alerted: bool,
    max_defer: float,
    cooldown: float,
) -> dict[str, bool]:
    """Decide whether to page/warn about autoDream staleness (pure, testable).

    The Telegram page fires only past ``max_defer + cooldown`` — i.e. after the
    loop's own self-heal force-run window has elapsed — so the operator is
    never paged before the system itself acts. A separate log-WARNING keeps the
    earlier 3×cooldown signal for dashboards. ``reset`` clears the page latch
    once staleness returns to healthy. Returns {"alert", "warn", "reset"}.
    """
    alert_threshold = max_defer + cooldown
    warn_threshold = cooldown * 3
    if staleness is None or staleness <= warn_threshold:
        return {"alert": False, "warn": False, "reset": True}
    if staleness > alert_threshold:
        return {"alert": not already_alerted, "warn": False, "reset": False}
    return {"alert": False, "warn": True, "reset": False}


def _autodream_defer_decision(
    agents_busy: bool,
    now: float,
    defer_started_at: float | None,
    max_defer: float,
) -> dict[str, Any]:
    """Decide whether to force a deferred autoDream run (pure, testable).

    Measures *continuous* busy time: the streak begins when agents first go
    busy and resets the moment they idle, so a long idle gap no longer
    masquerades as deferral. Returns {"force", "defer_started_at",
    "deferred_for"}.
    """
    if not agents_busy:
        return {"force": False, "defer_started_at": None, "deferred_for": 0.0}
    started = defer_started_at if defer_started_at is not None else now
    deferred_for = now - started
    return {
        "force": deferred_for >= max_defer,
        "defer_started_at": started,
        "deferred_for": deferred_for,
    }


async def _autodream_loop() -> None:
    """Background loop — triggers autoDream memory consolidation when engine is idle.

    Implements exponential backoff on consecutive errors (60s → 120s → ... → 3600s max).
    Resets to normal 60s interval on success.

    Max-defer ceiling: if running_agents() blocks the loop for longer than
    _AUTODREAM_MAX_DEFER_SECONDS (default 4h), autoDream fires anyway in
    post_stall mode so memory consolidation is never permanently starved.
    """
    global _autodream_defer_started_at  # noqa: PLW0603

    from robothor.engine.autodream import is_cooled_down, run_autodream
    from robothor.engine.dedup import running_agents

    consecutive_errors = 0

    while True:
        sleep_seconds = 60 if consecutive_errors == 0 else min(60 * 2**consecutive_errors, 3600)
        await asyncio.sleep(sleep_seconds)
        try:
            agents_busy = bool(running_agents())
            cooled = is_cooled_down()

            if agents_busy or not cooled:
                # Max-defer ceiling: force a run only after agents have been
                # *continuously* busy for _AUTODREAM_MAX_DEFER_SECONDS, tracked
                # in-process (not via the possibly-stale last-run timestamp).
                decision = _autodream_defer_decision(
                    agents_busy,
                    time.time(),
                    _autodream_defer_started_at,
                    _AUTODREAM_MAX_DEFER_SECONDS,
                )
                _autodream_defer_started_at = decision["defer_started_at"]
                if decision["force"]:
                    logger.warning(
                        "autoDream continuously deferred for %.1fh due to busy agents — "
                        "forcing DEEP run",
                        decision["deferred_for"] / 3600,
                    )
                    # Deep, not post_stall: only deep mode runs full lifecycle
                    # maintenance (importance scoring, decay, GC). Under sustained
                    # load a post_stall force would reset the staleness clock while
                    # deep maintenance silently never runs — the exact starvation
                    # this MAX_DEFER ceiling exists to prevent.
                    result = await run_autodream(mode="deep")
                    if result.get("skipped"):
                        # A concurrent run holds the lock. Back off instead of
                        # re-firing every 60s; keep the defer streak so we retry,
                        # and let that run's own _set_last_run_ts() end the streak.
                        logger.debug(
                            "autoDream force-run skipped (%s); backing off %ds",
                            result.get("reason"),
                            _AUTODREAM_FORCE_BACKOFF_SECONDS,
                        )
                        await asyncio.sleep(_AUTODREAM_FORCE_BACKOFF_SECONDS)
                        continue
                    _autodream_defer_started_at = None
                    consecutive_errors = 0
                continue

            await run_autodream(mode="idle")
            _autodream_defer_started_at = None
            consecutive_errors = 0
        except asyncio.CancelledError:
            return
        except Exception as e:
            consecutive_errors += 1
            logger.warning(
                "autoDream loop error (%d consecutive, next retry in %ds): %s",
                consecutive_errors,
                min(60 * 2**consecutive_errors, 3600),
                e,
            )


def run() -> None:
    """Entry point for python -m robothor.engine.daemon"""
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        return
    except Exception as e:
        logger.error("Engine crashed: %s", e, exc_info=True)
        sys.exit(1)
    if exit_code != 0:
        # A subsystem task died and triggered this shutdown. Exit non-zero so
        # systemd's OnFailure pager fires — exit 0 here means Restart=always
        # silently crash-loops the engine with no page, forever.
        sys.exit(exit_code)


if __name__ == "__main__":
    run()
