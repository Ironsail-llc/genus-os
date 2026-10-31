"""Ambient follow-ups: notice the threads waiting on us, and propose what to say.

An agent opts in with an ``ambient:`` manifest block (``docs/AMBIENT.md``). On
each ``digest_at`` time the scheduler runs one pass for it:

1. **Find stalled threads** in the sources the block watches (the CRM
   conversation inbox, or a Redis stream the agent already reads): a person
   wrote, ``stalled_after`` has passed, and nothing from us came after.
2. **Compose** the follow-ups with the agent's own run — its model, its
   instructions — on a ``cron`` trigger whose detail is ``ambient:<mode>``.
   That run delivers nothing and holds only read-only tools: what is sent is
   decided here, by tier, never by the composing run.
3. **Hold each proposal to its tier.** ``never`` kinds are not asked for, and
   dropped if the model writes one anyway. ``approve`` goes through the same
   approval gate a guarded tool call uses (the Telegram keyboard, the webchat
   card). ``auto`` sends.

``ROBOTHOR_AMBIENT`` is the ladder: ``off`` (default) does nothing at all,
``observe`` records every proposal as an ``ambient_proposal`` run step and in
a digest file and sends nothing, ``enforce`` sends per tier.

No migration: a proposal is a ``checkpoint`` step named ``ambient_proposal``
(the Runs view already renders those), and the run is a ``cron`` run with an
``ambient:`` trigger detail, the way heartbeat runs are.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.models import (
    AgentConfig,
    AgentRun,
    AmbientConfig,
    DeliveryMode,
    RunStep,
    StepType,
    TriggerType,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from apscheduler.triggers.base import BaseTrigger

    from robothor.engine.schedule_reconcile import JobSpec

__all__ = [
    "ACTION_KINDS",
    "DEFAULT_TIERS",
    "PROPOSAL_STEP",
    "AmbientConfig",
    "AmbientPass",
    "AmbientWorker",
    "Message",
    "Proposal",
    "StalledThread",
    "Thread",
    "ambient_trigger",
    "digest_at_error",
    "find_stalled",
    "parse_ambient",
    "parse_duration",
    "watch_source_error",
]

logger = logging.getLogger(__name__)

#: The scheduler job kind for an ambient pass (``schedule_reconcile`` imports it).
KIND_AMBIENT = "ambient"

NUDGE_THREAD = "nudge_thread"
SEND_DIGEST = "send_digest"
REPLY_ON_BEHALF = "reply_on_behalf"
ACTION_KINDS = (NUDGE_THREAD, SEND_DIGEST, REPLY_ON_BEHALF)

TIER_AUTO = "auto"
TIER_APPROVE = "approve"
TIER_NEVER = "never"
TIERS = (TIER_AUTO, TIER_APPROVE, TIER_NEVER)

#: Nothing goes out alone unless the manifest says so, and nothing is said in
#: the thread on our behalf unless the manifest says so.
DEFAULT_TIERS = {NUDGE_THREAD: TIER_APPROVE, SEND_DIGEST: TIER_APPROVE, REPLY_ON_BEHALF: TIER_NEVER}

#: ``tool_name`` of the checkpoint step each proposal is recorded as, and the
#: prefix of the tool name an approval card shows.
PROPOSAL_STEP = "ambient_proposal"
#: Guardrail name on the approval request, so a session grant ("Approve All")
#: covers ambient proposals and nothing else.
APPROVAL_GUARDRAIL = "ambient_tier"

#: What happened to a proposal; recorded on its step and in the digest.
OBSERVED = "observed"
SENT = "sent"
DENIED = "denied"
NO_APPROVER = "no_approver"
NO_TARGET = "no_target"
SEND_FAILED = "send_failed"

INBOX = "inbox"
STREAM_PREFIX = "stream:"
INBOUND = "inbound"
OUTBOUND = "outbound"

#: Read-only tool the composing run falls back to when the agent's own list
#: has none: an empty ``tools_allowed`` means EVERY tool, so it cannot be empty.
_INERT_TOOL = "wait_seconds"

# ── Manifest values ─────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_duration(value: Any) -> int | None:
    """Seconds in ``30m``, ``4h``, ``1d``, ``1h30m``; ``None`` if not a positive duration."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip().lower())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total or None


def _parse_times(value: str) -> list[tuple[int, int]]:
    times = []
    for part in value.split(","):
        match = _TIME_RE.match(part.strip())
        if not match:
            raise ValueError(f"{part.strip()!r} is not an HH:MM time")
        times.append((int(match.group(1)), int(match.group(2))))
    return times


def ambient_trigger(digest_at: str, timezone: Any = None) -> BaseTrigger:
    """The trigger for ``digest_at``: a 5-field cron, or comma-separated HH:MM times.

    Times become one ``CronTrigger`` each under an ``OrTrigger``, so
    ``"08:00, 17:30"`` fires at exactly those two times — a single cron with
    ``hour=8,17 minute=0,30`` would also fire at 08:30 and 17:00.
    """
    from apscheduler.triggers.combining import OrTrigger
    from apscheduler.triggers.cron import CronTrigger

    from robothor.engine.crontab import cron_trigger

    text = str(digest_at).strip()
    if ":" not in text:
        return cron_trigger(text, timezone=timezone)
    triggers = [CronTrigger(hour=h, minute=m, timezone=timezone) for h, m in _parse_times(text)]
    return triggers[0] if len(triggers) == 1 else OrTrigger(triggers)


def digest_at_error(value: str) -> str | None:
    """Why ``digest_at`` cannot be scheduled, or ``None`` when it can."""
    try:
        ambient_trigger(value, timezone="UTC")
    except Exception as error:  # noqa: BLE001 - any parse failure is the answer
        return str(error) or type(error).__name__
    return None


def ambient_schedule(agent: AgentConfig) -> str:
    """The ambient job's schedule, or ``""`` when the agent should have no job.

    No job at all while ``ROBOTHOR_AMBIENT`` is off: the flag's default must
    leave the scheduler exactly as it was, not register a job that wakes up
    to do nothing.
    """
    from robothor.engine.feature_flags import ambient_mode

    if agent.ambient is None or ambient_mode() == "off":
        return ""
    return agent.ambient.digest_at


def ambient_job_spec(agent: AgentConfig) -> JobSpec:
    """The one scheduler job an opted-in agent gets: ``<id>:ambient`` on ``digest_at``."""
    from robothor.engine.schedule_reconcile import JobSpec

    ambient = agent.ambient
    assert ambient is not None  # caller checks ambient_schedule first
    return JobSpec(
        job_id=f"{agent.id}:ambient",
        kind=KIND_AMBIENT,
        agent_id=agent.id,
        name=f"ambient:{agent.name}",
        trigger=ambient_trigger(ambient.digest_at, timezone=agent.timezone),
        cron_expr=ambient.digest_at,
        misfire_grace_time=300,
        enabled=agent.schedule_enabled,
        upsert={
            "cron_expr": ambient.digest_at,
            "timezone": agent.timezone,
            "timeout_seconds": agent.timeout_seconds,
            "model_primary": agent.model_primary,
            "model_fallbacks": agent.model_fallbacks,
            "delivery_mode": DeliveryMode.NONE.value,
            "delivery_channel": agent.delivery_channel,
            "delivery_to": agent.delivery_to,
            "session_target": "isolated",
        },
    )


def watch_source_error(source: str, streams_read: list[Any]) -> str | None:
    """Why the agent may not watch ``source``, or ``None`` when it may."""
    if source == INBOX:
        return None
    if source.startswith(STREAM_PREFIX):
        name = source.removeprefix(STREAM_PREFIX)
        if name and name in streams_read:
            return None
        return f"stream {name!r} is not in this agent's streams.read"
    return f"unknown watch source {source!r} (use 'inbox' or 'stream:<name>')"


def parse_ambient(raw: Any) -> AmbientConfig | None:
    """The manifest's ``ambient:`` block, or ``None`` when the agent has not opted in.

    Lenient on purpose — the validator is where a bad value is reported. A tier
    that is not one of the three keeps its default rather than becoming one.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    tiers = dict(DEFAULT_TIERS)
    raw_tiers = raw.get("tiers")
    if isinstance(raw_tiers, dict):
        tiers.update({k: v for k, v in raw_tiers.items() if k in ACTION_KINDS and v in TIERS})
    watch = raw.get("watch")
    stalled_after = str(raw.get("stalled_after") or "")
    return AmbientConfig(
        watch=[str(s) for s in watch] if isinstance(watch, list) else [],
        stalled_after=stalled_after,
        stalled_after_seconds=parse_duration(stalled_after) or 0,
        digest_at=str(raw.get("digest_at") or ""),
        tiers=tiers,
    )


# ── Threads and stalled detection ───────────────────────────────────


@dataclass(frozen=True)
class Message:
    direction: str  # INBOUND (a person) | OUTBOUND (us)
    at: datetime
    author: str = ""


@dataclass
class Thread:
    source: str
    thread_id: str
    messages: list[Message] = field(default_factory=list)
    person: str = ""
    subject: str = ""


@dataclass(frozen=True)
class StalledThread:
    source: str
    thread_id: str
    person: str
    subject: str
    #: When the wait began: the first inbound message after our last reply.
    waiting_since: datetime
    waited_seconds: int

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.thread_id}"


def find_stalled(
    threads: list[Thread], stalled_after_seconds: int, now: datetime
) -> list[StalledThread]:
    """Threads a person has waited on for at least ``stalled_after_seconds``, longest first.

    The clock starts at the first inbound message after our last reply, so a
    second "any news?" does not reset it, and a reply from us stops it.
    """
    stalled: list[StalledThread] = []
    for thread in threads:
        ordered = sorted(thread.messages, key=lambda m: m.at)
        last_ours = max((i for i, m in enumerate(ordered) if m.direction == OUTBOUND), default=-1)
        waiting = [m for m in ordered[last_ours + 1 :] if m.direction == INBOUND]
        if not waiting:
            continue
        since = waiting[0].at
        waited = int((now - since).total_seconds())
        if waited < stalled_after_seconds:
            continue
        stalled.append(
            StalledThread(
                source=thread.source,
                thread_id=thread.thread_id,
                person=thread.person or waiting[0].author,
                subject=thread.subject,
                waiting_since=since,
                waited_seconds=waited,
            )
        )
    stalled.sort(key=lambda s: s.waited_seconds, reverse=True)
    return stalled


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _inbox_threads(tenant_id: str) -> list[Thread]:
    """Open CRM conversations with their messages. Sync: call through a thread."""
    from robothor.crm.dal import list_conversations, list_messages

    threads = []
    for convo in list_conversations(status="open", page_size=100, tenant_id=tenant_id):
        rows = list_messages(int(convo["id"]), tenant_id=tenant_id)
        messages = []
        for row in rows if isinstance(rows, list) else []:
            kind = row.get("message_type")
            at = _aware(row.get("created_at"))
            if row.get("private") or at is None or kind not in ("incoming", "outgoing"):
                continue
            messages.append(Message(INBOUND if kind == "incoming" else OUTBOUND, at))
        threads.append(
            Thread(
                source=INBOX,
                thread_id=str(convo["id"]),
                messages=messages,
                person=str(convo.get("person_name") or convo.get("personName") or "").strip(),
                subject=str(convo.get("subject") or convo.get("title") or ""),
            )
        )
    return threads


def _stream_threads(stream: str) -> list[Thread]:
    """Threads in a Redis stream whose payloads carry ``thread_id`` and ``direction``."""
    from robothor.events.bus import read_recent

    threads: dict[str, Thread] = {}
    for event in read_recent(stream, count=500):
        payload = event.get("payload")
        at = _aware(event.get("timestamp"))
        if not isinstance(payload, dict) or at is None:
            continue
        thread_id, direction = payload.get("thread_id"), payload.get("direction")
        if not thread_id or direction not in (INBOUND, OUTBOUND):
            continue
        thread = threads.setdefault(
            str(thread_id), Thread(source=f"{STREAM_PREFIX}{stream}", thread_id=str(thread_id))
        )
        author = str(payload.get("person") or payload.get("from") or "")
        thread.messages.append(Message(direction, at, author))
        if direction == INBOUND and author and not thread.person:
            thread.person = author
    return list(threads.values())


async def load_watched_threads(agent: AgentConfig, tenant_id: str) -> list[Thread]:
    """Every thread in the sources the agent watches. One bad source costs only itself."""
    cfg = agent.ambient
    threads: list[Thread] = []
    for source in cfg.watch if cfg else []:
        try:
            if source == INBOX:
                threads += await asyncio.to_thread(_inbox_threads, tenant_id)
            elif watch_source_error(source, agent.streams_read) is None:
                stream = source.removeprefix(STREAM_PREFIX)
                threads += await asyncio.to_thread(_stream_threads, stream)
            else:
                logger.warning("ambient %s: skipping watch source %r", agent.id, source)
        except Exception as error:  # noqa: BLE001 - a source, not the pass
            logger.warning("ambient %s: could not read %s: %s", agent.id, source, error)
    return threads


# ── Composition ─────────────────────────────────────────────────────


@dataclass
class Proposal:
    kind: str
    tier: str
    text: str
    target: str  # a thread ref, or "digest"
    thread: StalledThread | None = None
    outcome: str = "pending"


_KIND_INSTRUCTIONS = {
    NUDGE_THREAD: (
        "### nudge_thread <thread ref>\n"
        "A short note to the operator: who is waiting, on what, for how long, "
        "and what the thread needs next. One section per thread worth nudging."
    ),
    REPLY_ON_BEHALF: (
        "### reply_on_behalf <thread ref>\n"
        "The reply we would post in that thread, written to the person who is waiting."
    ),
    SEND_DIGEST: (
        "### send_digest\nOne digest for the operator covering every waiting thread, longest first."
    ),
}


def allowed_kinds(tiers: dict[str, str]) -> list[str]:
    return [k for k in ACTION_KINDS if tiers.get(k, DEFAULT_TIERS[k]) != TIER_NEVER]


def _waited(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m"


def build_prompt(agent: AgentConfig, stalled: list[StalledThread], kinds: list[str]) -> str:
    """The composing run's message. Kinds whose tier is never are not mentioned at all."""
    lines = [
        f"Ambient follow-up pass for {agent.name or agent.id}.",
        "These threads are waiting on a reply from us, longest first:",
        "",
    ]
    for s in stalled:
        about = f' about "{s.subject}"' if s.subject else ""
        who = s.person or "someone"
        lines.append(
            f"- {s.ref}: {who}{about}, waiting {_waited(s.waited_seconds)} "
            f"since {s.waiting_since.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} UTC"
        )
    lines += [
        "",
        "Write only the sections below, each starting with its header line exactly as "
        "shown, and nothing else. Do not send anything yourself: what you write here is "
        "a proposal, and it is sent only if its tier allows.",
        "",
    ]
    lines += [_KIND_INSTRUCTIONS[k] for k in kinds]
    return "\n".join(lines)


_HEADER_RE = re.compile(
    r"^#{2,4}[ \t]*(" + "|".join(ACTION_KINDS) + r")\b[ \t]*(\S*)[ \t]*$", re.MULTILINE
)


def parse_proposals(
    output: str, stalled: list[StalledThread], kinds: list[str], tiers: dict[str, str]
) -> list[Proposal]:
    """The model's sections as proposals, keeping only what was asked for.

    A kind that was not asked for, a thread ref that is not one of the stalled
    threads, an empty body and a duplicate are all dropped. Output with no
    headers at all is taken as the digest when digests are allowed.
    """
    by_ref = {s.ref: s for s in stalled}
    headers = list(_HEADER_RE.finditer(output))
    if not headers:
        text = output.strip()
        if text and SEND_DIGEST in kinds:
            return [Proposal(SEND_DIGEST, tiers[SEND_DIGEST], text, "digest")]
        return []
    proposals: list[Proposal] = []
    seen: set[tuple[str, str]] = set()
    for i, header in enumerate(headers):
        kind, ref = header.group(1), header.group(2)
        end = headers[i + 1].start() if i + 1 < len(headers) else len(output)
        text = output[header.end() : end].strip()
        if kind not in kinds or not text:
            continue
        thread = None
        if kind == SEND_DIGEST:
            ref = "digest"
        else:
            thread = by_ref.get(ref)
            if thread is None:
                continue
        if (kind, ref) in seen:
            continue
        seen.add((kind, ref))
        proposals.append(Proposal(kind, tiers[kind], text, ref, thread))
    return proposals


def composing_config(agent: AgentConfig) -> AgentConfig:
    """The agent as it runs to compose: same model and instructions, no way to send.

    Delivery off, and tools cut to the read-only ones the agent already had.
    The worker decides what is sent; a composing run that could call a send
    tool would make ``observe`` a lie.
    """
    from robothor.engine.tools.constants import READONLY_TOOLS

    base = set(agent.tools_allowed) if agent.tools_allowed else set(READONLY_TOOLS)
    tools = sorted(base & READONLY_TOOLS) or [_INERT_TOOL]
    return dataclasses.replace(
        agent,
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=tools,
        tools_opt_in=[],
        auto_task=False,
        session_target="isolated",
    )


# ── The pass ────────────────────────────────────────────────────────


@dataclass
class AmbientPass:
    agent_id: str
    mode: str
    run_id: str | None = None
    stalled: list[StalledThread] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)


class AmbientWorker:
    """One ambient pass per call to :meth:`run_pass`. Every seam is injectable."""

    def __init__(
        self,
        runner: Any,
        *,
        tenant_id: str = DEFAULT_TENANT,
        load_threads: Callable[[AgentConfig], Awaitable[list[Thread]]] | None = None,
        channel_resolver: Callable[[str], Any] | None = None,
        approver: Callable[[], Any] | None = None,
        record_steps: Callable[[list[RunStep]], Awaitable[None]] | None = None,
        send_reply: Callable[[StalledThread, str], Awaitable[bool]] | None = None,
        digest_dir: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = runner
        self._tenant_id = tenant_id
        self._load_threads = load_threads or (lambda a: load_watched_threads(a, tenant_id))
        self._channel_resolver = channel_resolver or _default_channel
        self._approver = approver or _default_approver
        self._record_steps = record_steps or _default_record_steps
        self._send_reply = send_reply or (lambda t, text: _inbox_reply(t, text, tenant_id))
        self._digest_dir = digest_dir
        self._now = now or (lambda: datetime.now(UTC))

    async def run_pass(self, agent: AgentConfig) -> AmbientPass:
        from robothor.engine.feature_flags import ambient_mode

        mode = ambient_mode()
        result = AmbientPass(agent_id=agent.id, mode=mode)
        cfg = agent.ambient
        if mode == "off" or cfg is None:
            return result
        kinds = allowed_kinds(cfg.tiers)
        if not kinds or cfg.stalled_after_seconds <= 0:
            return result
        now = self._now()
        result.stalled = find_stalled(
            await self._load_threads(agent), cfg.stalled_after_seconds, now
        )
        if not result.stalled:
            logger.info("ambient %s (%s): nothing stalled", agent.id, mode)
            return result

        run: AgentRun = await self._runner.execute(
            agent_id=agent.id,
            message=build_prompt(agent, result.stalled, kinds),
            trigger_type=TriggerType.CRON,
            trigger_detail=f"ambient:{mode}",
            agent_config=composing_config(agent),
        )
        result.run_id = run.id
        result.proposals = parse_proposals(run.output_text or "", result.stalled, kinds, cfg.tiers)
        for proposal in result.proposals:
            proposal.outcome = (
                OBSERVED if mode == "observe" else await self._dispatch(agent, run, proposal)
            )
            logger.info(
                "ambient %s (%s): %s -> %s [%s, %s]",
                agent.id,
                mode,
                proposal.kind,
                proposal.target,
                proposal.tier,
                proposal.outcome,
            )
        await self._record(run, result.proposals, mode, now)
        await self._write_digest(agent, mode, result, now)
        return result

    async def _dispatch(self, agent: AgentConfig, run: AgentRun, proposal: Proposal) -> str:
        if proposal.tier == TIER_APPROVE:
            manager = self._approver()
            if manager is None:
                return NO_APPROVER
            approved = await manager.request_approval(
                agent_id=agent.id,
                run_id=run.id,
                tool_name=f"{PROPOSAL_STEP}:{proposal.kind}",
                tool_args={"target": proposal.target, "text": proposal.text},
                guardrail_name=APPROVAL_GUARDRAIL,
                reason=f"{proposal.kind} is on the approve tier in this agent's ambient block",
                timeout_seconds=agent.human_approval_timeout,
            )
            if not approved:
                return DENIED
        elif proposal.tier != TIER_AUTO:
            return "not_sent"
        return await self._send(agent, proposal)

    async def _send(self, agent: AgentConfig, proposal: Proposal) -> str:
        try:
            if proposal.kind == REPLY_ON_BEHALF:
                if proposal.thread is None:
                    return NO_TARGET
                delivered = await self._send_reply(proposal.thread, proposal.text)
            else:
                channel = self._channel_resolver(agent.delivery_channel)
                if channel is None or not agent.delivery_to:
                    return NO_TARGET
                receipt = await channel.send(agent.delivery_to, proposal.text)
                delivered = bool(getattr(receipt, "complete", receipt))
        except Exception as error:  # noqa: BLE001 - one proposal, not the pass
            logger.warning("ambient %s: %s send failed: %s", agent.id, proposal.kind, error)
            return SEND_FAILED
        return SENT if delivered else SEND_FAILED

    async def _record(
        self, run: AgentRun, proposals: list[Proposal], mode: str, now: datetime
    ) -> None:
        start = max((s.step_number for s in run.steps), default=0)
        steps = [
            RunStep(
                run_id=run.id,
                step_number=start + i,
                step_type=StepType.CHECKPOINT,
                tool_name=PROPOSAL_STEP,
                tool_input={
                    "kind": p.kind,
                    "tier": p.tier,
                    "target": p.target,
                    "mode": mode,
                    "waited_seconds": p.thread.waited_seconds if p.thread else None,
                },
                tool_output={"text": p.text, "outcome": p.outcome},
                started_at=now,
                completed_at=now,
                duration_ms=0,
            )
            for i, p in enumerate(proposals, start=1)
        ]
        if not steps:
            return
        try:
            await self._record_steps(steps)
        except Exception as error:  # noqa: BLE001 - the digest file still records it
            logger.warning("ambient: could not record steps for run %s: %s", run.id, error)

    async def _write_digest(
        self, agent: AgentConfig, mode: str, result: AmbientPass, now: datetime
    ) -> None:
        if not result.proposals:
            return
        lines = [
            f"## {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} UTC, ambient pass ({mode}), "
            f"run {result.run_id}",
            "",
        ]
        for p in result.proposals:
            lines.append(f"- {p.kind} -> {p.target} [{p.tier}, {p.outcome}]")
            lines += [f"    {line}" for line in p.text.splitlines()]
        directory = (self._digest_dir or _default_digest_dir()) / agent.id
        path = directory / f"{now.astimezone(UTC).strftime('%Y-%m-%d')}.md"
        try:
            await asyncio.to_thread(_append, path, "\n".join(lines) + "\n\n")
        except OSError as error:
            logger.warning("ambient %s: could not write digest %s: %s", agent.id, path, error)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _default_digest_dir() -> Path:
    from robothor.config import get_config

    return Path(get_config().workspace) / ".robothor" / "ambient"


def _default_channel(name: str) -> Any:
    from robothor.engine.channels.registry import get_channel

    return get_channel(name)


def _default_approver() -> Any:
    from robothor.engine.permission_escalation import get_permission_manager

    return get_permission_manager()


async def _default_record_steps(steps: list[RunStep]) -> None:
    from robothor.engine.tracking import create_steps_batch

    await asyncio.to_thread(create_steps_batch, steps)


async def _inbox_reply(thread: StalledThread, text: str, tenant_id: str) -> bool:
    """Post ``text`` as our reply in an inbox thread. Other sources cannot be replied to."""
    if thread.source != INBOX:
        return False
    from robothor.crm.dal import send_message

    sent = await asyncio.to_thread(
        send_message, int(thread.thread_id), text, "outgoing", False, tenant_id
    )
    return sent is not None
