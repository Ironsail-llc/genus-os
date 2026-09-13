"""What the manifests say the scheduler's job set should be — as data.

``scheduler.py`` used to answer that question twice, in two different shapes:
``start()`` built the jobs, and ``_reconcile_from_scan()`` built a bare set of
ids it was allowed to prune. Two derivations of one truth is how ``main:worker``
came to be pruned five minutes after every engine start — reconcile's id set had
simply forgotten a namespace ``start()`` knew about.

So the derivation lives here, once, and both call sites consume it. That also
makes the interesting part testable without a running scheduler: a
:class:`JobSpec` is a plain value, and the comparison that decides "same job or
replace it" is a pure function of two of them.

The comparison is the subtle bit. ``CronTrigger`` has no meaningful ``__eq__``
(two triggers for the same expression are different objects and compare
unequal), and ``str(trigger)`` renders the fields but NOT the timezone — so a
timezone-only edit is invisible to the obvious comparison and a nine-o'clock
agent keeps firing on the old zone forever. ``repr`` carries both, so ``repr``
is what is compared, together with the misfire grace, which is the other thing
a manifest edit can change about a live job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from robothor.engine.config import ManifestScan
    from robothor.engine.models import AgentConfig

#: Job kinds an agent manifest can contribute, in registration order.
KIND_AGENT = "agent"
KIND_HEARTBEAT = "heartbeat"
KIND_WORKER = "worker"

#: Per-kind wording for the "this cron will not parse" log line. Preserved
#: verbatim from the three separate sites this replaced: an operator has read
#: these before, and a reworded error is a silent change to a signal somebody
#: may already be grepping for.
CRON_ERROR_TEMPLATES = {
    KIND_AGENT: "Invalid cron expression for %s: %s — %s",
    KIND_HEARTBEAT: "Invalid heartbeat cron for %s: %s — %s",
    KIND_WORKER: "Invalid worker cron for %s: %s — %s",
}


@dataclass(frozen=True)
class JobSpec:
    """One APScheduler job an agent manifest asks for, fully resolved.

    ``kind`` rather than a bound method so this stays a value: the scheduler
    maps a kind to its own handler. A dataclass holding ``self._run_agent``
    would make every spec unprintable, uncomparable and impossible to build
    without an instance.
    """

    job_id: str
    kind: str
    agent_id: str
    name: str
    trigger: CronTrigger
    cron_expr: str
    misfire_grace_time: int | None
    #: ``schedule.enabled``. A spec is built either way: a disabled schedule
    #: still gets its ``agent_schedules`` row (saying ``enabled = false``, which
    #: is what the fleet view reads) and gets no APScheduler job.
    enabled: bool = True
    #: Keyword arguments for ``tracking.upsert_schedule``, minus ``tenant_id``
    #: and ``enabled``.
    upsert: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> tuple[str, int | None]:
        """Everything about this job a live APScheduler job can be compared to.

        ``repr(trigger)`` and not ``str(trigger)``: ``str`` omits the timezone.
        """
        return (repr(self.trigger), self.misfire_grace_time)

    def row(self) -> tuple[Any, ...]:
        """Everything that lands in ``agent_schedules``, comparably.

        Deliberately NOT part of :meth:`fingerprint`: the trigger decides
        whether the JOB has to be rebuilt, and this decides whether the ROW has
        to be rewritten. Conflating them either rebuilds a running job to
        change a model id, or — the way it actually went — leaves the row
        holding a model the manifest stopped naming, which is what the fleet
        view and `gen_cron_map.py` then report.
        """
        return (self.enabled, tuple(sorted((k, repr(v)) for k, v in self.upsert.items())))


@dataclass
class ReconcileResult:
    """What one reconcile pass actually did, and what it refused to do.

    Four lists rather than the single ``list[str]`` of pruned ids this replaced.
    The old return value could not distinguish "nothing to do" from "refused to
    do anything", which is the whole difference between a healthy fleet and a
    manifest directory nobody can read.
    """

    added: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    #: Jobs whose TRIGGER is unchanged but whose ``agent_schedules`` row was
    #: rewritten — a model, delivery or session-target edit. Its own list
    #: rather than folded into ``replaced``: nothing about the running job
    #: moved, and an operator reading "replaced" would go looking for a fire
    #: time that did not change.
    refreshed: list[str] = field(default_factory=list)
    #: agent id (or ``"*"`` for the whole directory) → why it was skipped.
    #: Error TYPES and ids only — never a path, a filename or a manifest value.
    blocked: dict[str, str] = field(default_factory=dict)
    clean: bool = True

    def as_dict(self) -> dict[str, Any]:
        """The JSON body of ``POST /api/admin/scheduler/reconcile``."""
        return {
            "added": sorted(self.added),
            "replaced": sorted(self.replaced),
            "pruned": sorted(self.pruned),
            "refreshed": sorted(self.refreshed),
            "blocked": dict(self.blocked),
            "clean": self.clean,
        }

    def touched(self) -> int:
        return len(self.added) + len(self.replaced) + len(self.pruned) + len(self.refreshed)


class RowLedger:
    """What this process last wrote to ``agent_schedules``, per job.

    The question "does the row need rewriting" is not the question "does the job
    need rebuilding", and answering only the second left a model- or
    delivery-only edit reconciling to a silent no-op: the trigger had not moved,
    so nothing was written, and the row kept a model the manifest had stopped
    naming. The fleet view, ``routers/agents.py`` and ``gen_cron_map.py`` all
    read those columns, so the appliance's own state table disagreed with its
    manifests until the next restart.

    A ledger rather than "just upsert every spec every pass": reconcile runs
    every five minutes for the life of the process, and rewriting every row each
    time is a write loop wearing a reconcile's name. ``forget`` on a failed
    write is what keeps the next pass trying rather than believing a row it
    never managed to store.
    """

    def __init__(self) -> None:
        self._rows: dict[str, tuple[Any, ...]] = {}

    def needs_write(self, spec: JobSpec) -> bool:
        return self._rows.get(spec.job_id) != spec.row()

    def record(self, spec: JobSpec) -> None:
        self._rows[spec.job_id] = spec.row()

    def forget(self, job_id: str) -> None:
        self._rows.pop(job_id, None)


def blocked_reasons(scan: ManifestScan) -> dict[str, str]:
    """Why a dirty scan is authority for nothing, per agent.

    ``ManifestFailure.filename`` is a bare filename and ``detail`` is a parser
    message — both are instance data by CLAUDE.md rules 1 and 2, and this
    mapping is answered to a browser. Only the agent id (falling back to the
    file's stem, which is the id by convention) and the error TYPE come out.
    """
    if not scan.dir_readable:
        return {"*": "manifest directory unreadable"}
    return {(f.agent_id or Path(f.filename).stem): f.error_type for f in scan.failures}


def _cron_spec(
    agent: AgentConfig,
    *,
    kind: str,
    cron_expr: str,
    timezone: str,
    job_id: str,
    name: str,
    misfire_grace_time: int | None,
    upsert: dict[str, Any],
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        kind=kind,
        agent_id=agent.id,
        name=name,
        trigger=CronTrigger.from_crontab(cron_expr, timezone=timezone),
        cron_expr=cron_expr,
        misfire_grace_time=misfire_grace_time,
        enabled=agent.schedule_enabled,
        upsert=upsert,
    )


def _heartbeat_spec(agent: AgentConfig) -> JobSpec:
    hb = agent.heartbeat
    assert hb is not None  # caller checks; keeps mypy and the reader honest
    return _cron_spec(
        agent,
        kind=KIND_HEARTBEAT,
        cron_expr=hb.cron_expr,
        timezone=hb.timezone,
        job_id=f"{agent.id}:heartbeat",
        name=f"heartbeat:{agent.name}",
        misfire_grace_time=60,
        upsert={
            "cron_expr": hb.cron_expr,
            "timezone": hb.timezone,
            "timeout_seconds": hb.timeout_seconds,
            "model_primary": agent.model_primary,
            "model_fallbacks": agent.model_fallbacks,
            "delivery_mode": hb.delivery_mode.value,
            "delivery_channel": hb.delivery_channel,
            "delivery_to": hb.delivery_to,
            "session_target": hb.session_target,
        },
    )


def _worker_spec(agent: AgentConfig) -> JobSpec:
    worker = agent.worker
    assert worker is not None
    return _cron_spec(
        agent,
        kind=KIND_WORKER,
        cron_expr=worker.cron_expr,
        timezone=worker.timezone,
        job_id=f"{agent.id}:worker",
        name=f"worker:{agent.name}",
        misfire_grace_time=120,
        upsert={
            "cron_expr": worker.cron_expr,
            "timezone": worker.timezone,
            "timeout_seconds": worker.timeout_seconds,
            "model_primary": agent.model_primary,
            "model_fallbacks": agent.model_fallbacks,
            "delivery_mode": worker.delivery_mode.value,
            "delivery_channel": worker.delivery_channel,
            "delivery_to": worker.delivery_to,
            "session_target": worker.session_target,
        },
    )


def _agent_spec(agent: AgentConfig) -> JobSpec:
    # APScheduler's own misfire handling is the catch-up policy: a grace of
    # None runs every missed fire, a finite one drops fires older than the
    # staleness budget.
    grace = agent.stale_after_minutes * 60 if agent.catch_up == "skip_if_stale" else None
    return _cron_spec(
        agent,
        kind=KIND_AGENT,
        cron_expr=agent.cron_expr,
        timezone=agent.timezone,
        job_id=agent.id,
        name=f"agent:{agent.name}",
        misfire_grace_time=grace,
        upsert={
            "cron_expr": agent.cron_expr,
            "timezone": agent.timezone,
            "timeout_seconds": agent.timeout_seconds,
            "model_primary": agent.model_primary,
            "model_fallbacks": agent.model_fallbacks,
            "delivery_mode": agent.delivery_mode.value,
            "delivery_channel": agent.delivery_channel,
            "delivery_to": agent.delivery_to,
            "session_target": agent.session_target,
        },
    )


def agent_job_specs(
    agent: AgentConfig,
) -> tuple[dict[str, JobSpec], list[tuple[str, str, str, Exception]]]:
    """``({job_id: spec}, [(kind, agent_id, cron_expr, error)])`` for one agent.

    An unparseable cron costs that ONE job, never the others and never the
    fleet: the caller logs it and moves on, exactly as the three hand-written
    blocks this replaced did.

    ``schedule.enabled`` rides on every spec rather than filtering here. A
    disabled agent still declares its schedules — the caller registers no job
    for them and writes ``enabled = false`` on the row — because deleting the
    row instead would make a silenced agent indistinguishable from a deleted
    one on every surface that reads ``agent_schedules``.
    """
    specs: dict[str, JobSpec] = {}
    errors: list[tuple[str, str, str, Exception]] = []

    builders = (
        (KIND_HEARTBEAT, agent.heartbeat and agent.heartbeat.cron_expr, _heartbeat_spec),
        (KIND_WORKER, agent.worker and agent.worker.cron_expr, _worker_spec),
        (KIND_AGENT, agent.cron_expr, _agent_spec),
    )
    for kind, cron_expr, build in builders:
        if not cron_expr:
            continue
        try:
            spec = build(agent)
        except Exception as error:  # noqa: BLE001 — one bad cron, not all of them
            errors.append((kind, agent.id, str(cron_expr), error))
            continue
        specs[spec.job_id] = spec
    return specs, errors


def desired_job_specs(
    agents: list[AgentConfig],
) -> tuple[dict[str, JobSpec], list[tuple[str, str, str, Exception]]]:
    """Every job the fleet's manifests declare, keyed by job id."""
    specs: dict[str, JobSpec] = {}
    errors: list[tuple[str, str, str, Exception]] = []
    for agent in agents:
        agent_specs, agent_errors = agent_job_specs(agent)
        specs.update(agent_specs)
        errors.extend(agent_errors)
    return specs, errors


def job_matches(job: Any, spec: JobSpec) -> bool:
    """True when a live APScheduler job already is what the manifest asks for.

    Compared on ``repr(trigger)`` plus the misfire grace. See the module
    docstring: ``CronTrigger.__eq__`` is identity and ``str(trigger)`` drops
    the timezone, so both of the obvious comparisons report "unchanged" for an
    edit that changes when the agent runs.
    """
    return (repr(getattr(job, "trigger", None)), getattr(job, "misfire_grace_time", None)) == (
        spec.fingerprint()
    )
