"""Run the queued reviews, several at once: the ``pr_review_work`` tool. No model.

It replaced the pr-reviewer agent's run loop, which took one task at a time
and woke at most every two minutes (18 pull requests posted together were
answered over 1 h 45 min, 2026-10-13). The per-task steps are the agent
brief's, unchanged:

1. mark the task ``IN_PROGRESS``;
2. ``prepare`` — it starts the review job itself. ``skip`` resolves the task
   with the reason, ``error`` with ``Prepare failed: <error>``;
   ``already_started`` carries on with the job an earlier attempt bound;
   ``resumed`` (only posting failed) skips the wait;
3. wait for the job until it is done, failed or cancelled;
4. ``finalize`` the job prepare returned — it posts, or records the failure;
5. resolve the task with ``<verdict>: <review_url>`` or ``<status>: <error>``.

**Concurrency.** Up to ``cfg.workers`` tasks at once, oldest first. While
anything is in flight the queue is re-read every ``poll_s``, so a task filed
mid-run starts as soon as a slot frees. No new task is taken after
``take_for_s``; work still in flight at ``deadline_s`` is left as it is — its
task stays ``IN_PROGRESS`` and the next run takes it again, where
``prepare`` hands back the job already bound, so a cut-short run never pays
for a second review. A job that answers "still running" at once — this
engine holds no live runner for it — is looked at every ``poll_s``, never in
a loop, and after ``stalled_s`` its task is handed back the same way.

**CRM errors.** A task the CRM refuses to start is not reviewed; one it
refuses to resolve keeps its outcome, with ``error`` saying so (the review is
never posted twice: finalize returns ``already_posted``).

**Digest.** A non-empty ``digest`` from finalize (a posted review with the
Telegram digest on, or a review the operator asked for) goes to ``notify`` as
it arrives.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.pr_review.config import ReviewerConfig

    Prepare = Callable[[str, int], Awaitable[dict[str, Any]]]
    #: ``(job_id, timeout_s) -> job status`` ("" when the job is gone).
    Wait = Callable[[str, float], Awaitable[str]]
    Finalize = Callable[[str, int, str], Awaitable[dict[str, Any]]]
    Notify = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)

__all__ = ["DEADLINE_SECONDS", "TAKE_FOR_SECONDS", "TaskQueue", "parse_task", "work"]

#: No new task after this long: a review takes 3–8 minutes, so the work in
#: flight finishes inside the run.
TAKE_FOR_SECONDS = 45 * 60
#: The run returns by then, whatever is in flight — inside the pr-review-run
#: workflow's 3300 s tool step and its 3600 s timeout.
DEADLINE_SECONDS = 54 * 60
POLL_SECONDS = 30.0
#: How long a job may keep answering "still running" at once — the sign this
#: engine holds no live runner for it (HA, a failed resume, an orphan row) —
#: before its task is handed back, ``IN_PROGRESS``, to the orphan path.
STALLED_SECONDS = 300.0
#: One wait call; the loop re-checks the deadline between them.
_WAIT_CHUNK = 300.0
_RUNNING_JOB = frozenset({"queued", "running"})
_ERROR_MAX = 300
_LINE_RE = re.compile(r"^(repo|number):\s*(\S+)\s*$", re.MULTILINE)


class TaskQueue(Protocol):
    async def list_open(self) -> list[dict[str, Any]]: ...
    async def start(self, task_id: str) -> None: ...
    async def resolve(self, task_id: str, resolution: str) -> None: ...


@dataclass
class Outcome:
    task_id: str
    pr: str
    status: str
    head_sha: str = ""
    verdict: str = ""
    review_url: str = ""
    resolution: str = ""
    #: A CRM write that failed (the task may still be open); the review itself
    #: is never re-posted for it — finalize returns ``already_posted``.
    error: str = ""


def parse_task(task: dict[str, Any]) -> tuple[str, int] | None:
    """``(repo, number)`` from a review task's ``repo:`` and ``number:`` lines."""
    found = {m.group(1): m.group(2) for m in _LINE_RE.finditer(str(task.get("body") or ""))}
    repo, number = found.get("repo", ""), found.get("number", "")
    if "/" not in repo or not number.isdigit():
        return None
    return repo, int(number)


def _short(text: Any) -> str:
    return str(text or "")[:_ERROR_MAX]


class _Run:
    def __init__(
        self,
        cfg: ReviewerConfig,
        queue: TaskQueue,
        prepare: Prepare,
        wait: Wait,
        finalize: Finalize,
        notify: Notify | None,
        deadline: float,
        clock: Callable[[], float],
        poll_s: float,
        stalled_s: float,
    ) -> None:
        self.cfg = cfg
        self.queue = queue
        self.prepare = prepare
        self.wait = wait
        self.finalize = finalize
        self.notify = notify
        self.deadline = deadline
        self.clock = clock
        self.poll_s = poll_s
        self.stalled_s = stalled_s
        self.digest: list[str] = []

    async def resolve(self, out: Outcome, resolution: str) -> Outcome:
        out.resolution = resolution
        try:
            await self.queue.resolve(out.task_id, resolution)
        except Exception as exc:  # noqa: BLE001 - surfaced; the outcome stands
            out.error = f"task not resolved: {_short(exc)}"
            logger.warning("pr_review_work %s: %s", out.task_id, out.error)
        return out

    async def wait_for(self, job_id: str) -> str:
        """The job's final status, or "" when it is still running at the
        deadline, or "stalled" when this engine is not running it."""
        status, stalled_since = "queued", None
        while status in _RUNNING_JOB:
            left = self.deadline - self.clock()
            if left <= 0:
                return ""
            chunk = min(left, _WAIT_CHUNK)
            asked = self.clock()
            status = await self.wait(job_id, chunk)
            if status not in _RUNNING_JOB or self.clock() - asked >= chunk / 2:
                stalled_since = None
                continue
            # Back at once and still running: no live runner here. Pace the
            # next look instead of spinning, and hand it back if it persists.
            stalled_since = stalled_since if stalled_since is not None else asked
            if self.clock() - stalled_since >= self.stalled_s:
                return "stalled"
            await asyncio.sleep(max(0.0, min(self.poll_s, self.deadline - self.clock())))
        return status

    async def one(self, task: dict[str, Any]) -> Outcome:
        task_id = str(task["id"])
        if str(task.get("status") or "TODO") != "IN_PROGRESS":
            try:
                await self.queue.start(task_id)  # a task resolves only from IN_PROGRESS
            except Exception as exc:  # noqa: BLE001 - not this run's to take
                return Outcome(task_id, "", "not_started", error=f"task not started: {exc}")
        parsed = parse_task(task)
        if parsed is None:
            out = Outcome(task_id, "", "unreadable")
            return await self.resolve(out, "Not a review task: no repo/number lines")
        repo, number = parsed
        out = Outcome(task_id, f"{repo}#{number}", "")
        try:
            prepared = await self.prepare(repo, number)
        except Exception as exc:  # noqa: BLE001 - one task never stops the others
            prepared = {"error": f"{type(exc).__name__}: {exc}"}
        out.head_sha = str(prepared.get("head_sha") or "")
        if prepared.get("skip"):
            out.status = "skipped"
            return await self.resolve(out, _short(prepared.get("reason") or "skipped"))
        job_id = str(prepared.get("job_id") or "")
        if prepared.get("error") or not job_id:
            out.status = "prepare_failed"
            error = prepared.get("error") or "no job id"
            return await self.resolve(out, f"Prepare failed: {_short(error)}")
        if not prepared.get("resumed"):
            ended = await self.wait_for(job_id)
            if ended in ("", "stalled"):
                # Left IN_PROGRESS: the next run takes it, and prepare hands
                # back this job instead of starting another.
                out.status = "unfinished"
                out.resolution = (
                    "job has no runner in this engine; left for the orphan path"
                    if ended == "stalled"
                    else "deadline reached; the next run finishes it"
                )
                return out
        done = await self.finalize(repo, number, job_id)
        digest = str(done.get("digest") or "")
        if digest:
            self.digest.append(digest)
            if self.notify is not None:
                try:
                    await self.notify(digest)
                except Exception as exc:  # noqa: BLE001 - the review is posted either way
                    logger.warning("pr_review digest not sent for %s: %s", out.pr, exc)
        status = str(done.get("status") or "")
        if status == "posted":
            out.status = "posted"
            out.verdict = str(done.get("verdict") or "")
            out.review_url = str(done.get("review_url") or "")
            return await self.resolve(out, f"{out.verdict}: {out.review_url}")
        if status:
            out.status = status
            return await self.resolve(
                out, f"{status}: {_short(done.get('error') or done.get('next'))}"
            )
        out.status = "finalize_failed"
        return await self.resolve(out, f"Finalize failed: {_short(done.get('error'))}")


async def work(
    cfg: ReviewerConfig,
    queue: TaskQueue,
    *,
    prepare: Prepare,
    wait: Wait,
    finalize: Finalize,
    notify: Notify | None = None,
    take_for_s: float = TAKE_FOR_SECONDS,
    deadline_s: float = DEADLINE_SECONDS,
    poll_s: float = POLL_SECONDS,
    stalled_s: float = STALLED_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Drain the open review tasks, ``cfg.workers`` at a time; the run's outcomes."""
    started = clock()
    run = _Run(
        cfg, queue, prepare, wait, finalize, notify, started + deadline_s, clock, poll_s, stalled_s
    )
    running: dict[asyncio.Task[Outcome], str] = {}
    seen: set[str] = set()
    outcomes: list[Outcome] = []
    workers = max(1, cfg.workers)
    try:
        while True:
            if clock() - started < take_for_s and len(running) < workers:
                for task in await queue.list_open():
                    task_id = str(task.get("id") or "")
                    if len(running) >= workers:
                        break
                    if not task_id or task_id in seen:
                        continue
                    seen.add(task_id)
                    running[asyncio.create_task(run.one(task))] = task_id
            if not running:
                break
            finished, _ = await asyncio.wait(
                running, timeout=poll_s, return_when=asyncio.FIRST_COMPLETED
            )
            for done in finished:
                task_id = running.pop(done)
                try:
                    outcomes.append(done.result())
                except Exception as exc:  # noqa: BLE001 - recorded; the task stays open
                    logger.warning("pr_review_work task %s raised: %s", task_id, exc)
                    outcomes.append(Outcome(task_id, "", "error", resolution=_short(exc)))
    finally:
        # Cancelled from outside (the step's timeout): stop the waits too. The
        # review jobs run on in the coding manager; their tasks stay open for
        # the next run.
        for pending in running:
            pending.cancel()
    if outcomes and cfg.status_file:
        await _write_status(Path(cfg.status_file), outcomes)
    return {
        "processed": len(outcomes),
        "posted": sum(1 for o in outcomes if o.status == "posted"),
        "unfinished": sum(1 for o in outcomes if o.status in ("unfinished", "error")),
        "task_errors": [f"{o.task_id}: {o.error}" for o in outcomes if o.error],
        "outcomes": [asdict(o) for o in outcomes],
        "digest": run.digest,
        "elapsed_s": round(clock() - started, 1),
    }


async def _write_status(path: Path, outcomes: list[Outcome]) -> None:
    """The status file the agent used to write (warmup context for main)."""
    lines = [
        "# PR Reviewer Status",
        f"Last run: {datetime.now(UTC).isoformat(timespec='seconds')}",
    ]
    for o in outcomes:
        what = o.verdict or o.status
        tail = o.review_url or o.resolution
        lines.append(f"- {o.pr or o.task_id} @{o.head_sha[:7]}: {what} {tail}".rstrip())

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    try:
        await asyncio.to_thread(_write)
    except OSError as exc:
        logger.warning("pr_review status file %s not written: %s", path, exc)
