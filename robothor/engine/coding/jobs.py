"""Durable Claude Code jobs and the loop that sees each one through.

**Claude Code's word is never the verdict.** When a round ends, the job runs
the acceptance command itself, in the worktree, and checks for a new commit on
the job's branch with a clean tree. Only both together mark the job ``done``.
Anything less resumes the SAME Claude Code session (``--resume``) with the
failing output, up to ``max_rounds`` rounds and ``max_budget_usd`` dollars.
The evidence a ``done`` job carries — verify exit code, an output hash, the
commit sha — is recorded in the shape :mod:`robothor.engine.session_goal`
validates, so a goal can cite it rather than an agent's say-so.

**Jobs belong to the engine, not to a run.** Each job is an asyncio task the
manager owns, so an orchestrator turn can start one, end, and pick it up later
with ``claude_code_wait``. Rows live in the ``coding_jobs`` table (migration
``144_coding_jobs``). On engine start, :func:`resume_interrupted_jobs` finds the
rows still ``queued``/``running`` and continues them — with ``--resume
<session_id>`` when Claude Code had already announced its session, which the
job records the moment it does so, not at the end of the round. A resume counts
as a round. Engine shutdown cancels the tasks WITHOUT marking the rows, which is
what makes them resumable; only ``claude_code_cancel`` marks a job cancelled.

**Concurrency is capped per tenant** (``ROBOTHOR_CODING_MAX_CONCURRENT``,
default 2). A job over the cap waits as ``queued``.

**The acceptance command** runs as ``shlex.split`` + ``create_subprocess_exec``
— no shell — in the worktree, with an environment holding no credential at all
(:func:`robothor.engine.coding.env.build_verify_env`), in its own process group
with a timeout. A pipeline needs an explicit ``bash -c '…'``. It is chosen by
the orchestrating agent, and that is acceptable because it can do nothing a
``code``-mode Claude Code session in the same worktree could not already do.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import logging
import os
import re
import shlex
import signal
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from robothor.engine.coding import env as coding_env
from robothor.engine.coding import sandbox
from robothor.engine.coding import worktree as wt
from robothor.engine.coding.runner import (
    ClaudeInvocation,
    ClaudeResult,
    ProgressEvent,
    check_effort,
    check_mode,
    run_claude,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Acceptance",
    "CodingJob",
    "CodingJobManager",
    "JobStatus",
    "MemoryJobStore",
    "PgJobStore",
    "check_repo_path",
    "get_manager",
    "resume_interrupted_jobs",
    "use_manager",
]

DEFAULT_MAX_ROUNDS = 3
MAX_MAX_ROUNDS = 10
_EVENTS_TAIL = 30
_OUTPUT_TAIL = 3000
_PROMPT_OUTPUT_TAIL = 2500
_REAP_INTERVAL_S = 3600.0

#: Told to every session. The task prompt says what to do; this says how to
#: behave when nobody is there to ask.
SYSTEM_PROMPT = (
    "You are running headless as a delegated coding worker for Genus OS. No human "
    "will answer questions: make reasonable decisions and finish the task. Work only "
    "inside the current repository. Never push, never switch or create branches, "
    "never rewrite history. Your work is checked independently after you stop, so "
    "run the acceptance command yourself before you finish."
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})
UNFINISHED = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _settings() -> Any:
    from robothor.settings import get_settings

    return get_settings().coding


@dataclass
class Acceptance:
    """What ``done`` means for a job."""

    verify_command: str | None = None
    require_commit: bool = True
    #: Read-only jobs only: one more round in the same session with this
    #: instruction, when the first finished round used fewer than
    #: ``second_pass_below_turns`` turns (always, when that is None). The job's
    #: answer is the second round's; a second round that returns nothing
    #: leaves the first answer in place.
    second_pass: str | None = None
    second_pass_below_turns: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verify_command": self.verify_command,
            "require_commit": self.require_commit,
            "second_pass": self.second_pass,
            "second_pass_below_turns": self.second_pass_below_turns,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Acceptance:
        raw = raw or {}
        cmd = raw.get("verify_command")
        second = str(raw.get("second_pass") or "").strip() or None
        below = raw.get("second_pass_below_turns")
        return cls(
            verify_command=str(cmd).strip() or None if cmd else None,
            require_commit=bool(raw.get("require_commit", True)),
            second_pass=second,
            second_pass_below_turns=int(below) if below is not None else None,
        )


@dataclass
class CodingJob:
    id: str
    tenant_id: str
    agent_id: str
    task: str
    repo_path: str
    mode: str
    acceptance: Acceptance
    worktree_path: str = ""
    branch: str | None = None
    base_ref: str = "HEAD"
    base_sha: str = ""
    model: str | None = None
    #: Per-job ``--effort`` / ``--max-turns`` / round timeout; None uses the
    #: instance's coding settings (the CLI default for effort).
    effort: str | None = None
    max_turns: int | None = None
    round_timeout_s: float | None = None
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_budget_usd: float | None = None
    grant_github: bool = False
    json_schema: dict[str, Any] | None = None
    run_id: str = ""
    goal_id: str | None = None
    task_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    session_id: str | None = None
    rounds: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    events_tail: list[str] = field(default_factory=list)
    pending_followups: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    finished_at: str | None = None

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return list(self.result.get("evidence") or [])

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


# ── Stores ────────────────────────────────────────────────────────────


class JobStore(Protocol):
    async def insert(self, job: CodingJob) -> None: ...
    async def save(self, job: CodingJob) -> None: ...
    async def get(self, job_id: str, tenant_id: str) -> CodingJob | None: ...
    async def list_unfinished(self, tenant_id: str | None = None) -> list[CodingJob]: ...
    async def list_reapable(self, finished_before: str) -> list[CodingJob]: ...


def _reapable(job: CodingJob, finished_before: str) -> bool:
    return (
        job.status in TERMINAL
        and bool(job.finished_at)
        and str(job.finished_at) < finished_before
        and not job.result.get("reaped_at")
    )


class MemoryJobStore:
    """An in-process store. For tests, and for an instance with no database."""

    def __init__(self) -> None:
        self._rows: dict[str, CodingJob] = {}

    async def insert(self, job: CodingJob) -> None:
        self._rows[job.id] = copy.deepcopy(job)

    async def save(self, job: CodingJob) -> None:
        self._rows[job.id] = copy.deepcopy(job)

    async def get(self, job_id: str, tenant_id: str) -> CodingJob | None:
        row = self._rows.get(job_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return copy.deepcopy(row)

    async def list_unfinished(self, tenant_id: str | None = None) -> list[CodingJob]:
        return [
            copy.deepcopy(r)
            for r in self._rows.values()
            if r.status in UNFINISHED and (tenant_id is None or r.tenant_id == tenant_id)
        ]

    async def list_reapable(self, finished_before: str) -> list[CodingJob]:
        return [copy.deepcopy(r) for r in self._rows.values() if _reapable(r, finished_before)]


_COLUMNS = (
    "id",
    "tenant_id",
    "agent_id",
    "run_id",
    "goal_id",
    "task_id",
    "repo_path",
    "worktree_path",
    "branch",
    "base_ref",
    "base_sha",
    "mode",
    "model",
    "effort",
    "max_turns",
    "round_timeout_s",
    "task",
    "acceptance",
    "json_schema",
    "max_rounds",
    "max_budget_usd",
    "grant_github",
    "status",
    "session_id",
    "rounds",
    "cost_usd",
    "turns",
    "events_tail",
    "pending_followups",
    "result",
    "error",
    "created_at",
    "updated_at",
    "finished_at",
)
_MUTABLE = (
    "worktree_path",
    "branch",
    "base_sha",
    "max_rounds",
    "status",
    "session_id",
    "rounds",
    "cost_usd",
    "turns",
    "events_tail",
    "pending_followups",
    "result",
    "error",
    "updated_at",
    "finished_at",
)
_JSON_COLUMNS = frozenset(
    {"acceptance", "json_schema", "events_tail", "pending_followups", "result"}
)


def _row_values(job: CodingJob, columns: tuple[str, ...]) -> list[Any]:
    from psycopg2.extras import Json

    values: list[Any] = []
    for col in columns:
        value: Any = job.acceptance.to_dict() if col == "acceptance" else getattr(job, col)
        if col == "status":
            value = str(value)
        if col in _JSON_COLUMNS:
            value = Json(value)
        values.append(value)
    return values


def _job_from_row(row: dict[str, Any]) -> CodingJob:
    data = dict(row)
    data["id"] = str(data["id"])
    data["acceptance"] = Acceptance.from_dict(data.get("acceptance"))
    data["status"] = JobStatus(data["status"])
    for col in ("created_at", "updated_at", "finished_at"):
        if data.get(col) is not None and not isinstance(data[col], str):
            data[col] = data[col].isoformat()
    for col in ("cost_usd", "max_budget_usd", "round_timeout_s"):
        if data.get(col) is not None:
            data[col] = float(data[col])
    data["events_tail"] = list(data.get("events_tail") or [])
    data["pending_followups"] = list(data.get("pending_followups") or [])
    data["result"] = dict(data.get("result") or {})
    return CodingJob(**{k: data[k] for k in _COLUMNS if k in data})


class PgJobStore:
    """The ``coding_jobs`` table, tenant-scoped through the RLS binding."""

    def _connect(self, tenant_id: str | None) -> Any:
        from robothor.db.connection import get_connection, tenant_scope

        if tenant_id is None:
            return get_connection()

        @contextlib.contextmanager
        def _scoped() -> Any:
            with tenant_scope(tenant_id), get_connection() as conn:
                yield conn

        return _scoped()

    def _insert(self, job: CodingJob) -> None:
        cols = ", ".join(_COLUMNS)
        marks = ", ".join(["%s"] * len(_COLUMNS))
        with self._connect(job.tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO coding_jobs ({cols}) VALUES ({marks})", _row_values(job, _COLUMNS)
            )
            conn.commit()

    def _save(self, job: CodingJob) -> None:
        sets = ", ".join(f"{c} = %s" for c in _MUTABLE)
        with self._connect(job.tenant_id) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE coding_jobs SET {sets} WHERE id = %s AND tenant_id = %s",
                [*_row_values(job, _MUTABLE), job.id, job.tenant_id],
            )
            conn.commit()

    def _get(self, job_id: str, tenant_id: str) -> CodingJob | None:
        from psycopg2.extras import RealDictCursor

        try:
            uuid.UUID(job_id)
        except ValueError:
            return None
        with self._connect(tenant_id) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM coding_jobs WHERE id = %s AND tenant_id = %s",
                (job_id, tenant_id),
            )
            row = cur.fetchone()
        return _job_from_row(row) if row else None

    def _list_unfinished(self, tenant_id: str | None) -> list[CodingJob]:
        from psycopg2.extras import RealDictCursor

        sql = f"SELECT {', '.join(_COLUMNS)} FROM coding_jobs WHERE status IN ('queued', 'running')"
        params: tuple[Any, ...] = ()
        if tenant_id is not None:
            sql += " AND tenant_id = %s"
            params = (tenant_id,)
        with self._connect(tenant_id) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql + " ORDER BY created_at", params)
            rows = cur.fetchall()
        return [_job_from_row(r) for r in rows]

    def _list_reapable(self, finished_before: str) -> list[CodingJob]:
        from psycopg2.extras import RealDictCursor

        sql = (
            f"SELECT {', '.join(_COLUMNS)} FROM coding_jobs "
            "WHERE status IN ('done', 'failed', 'cancelled') AND finished_at < %s "
            "AND COALESCE(result->>'reaped_at', '') = '' ORDER BY finished_at LIMIT 200"
        )
        with self._connect(None) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (finished_before,))
            rows = cur.fetchall()
        return [_job_from_row(r) for r in rows]

    async def insert(self, job: CodingJob) -> None:
        await asyncio.to_thread(self._insert, copy.deepcopy(job))

    async def save(self, job: CodingJob) -> None:
        await asyncio.to_thread(self._save, copy.deepcopy(job))

    async def get(self, job_id: str, tenant_id: str) -> CodingJob | None:
        return await asyncio.to_thread(self._get, job_id, tenant_id)

    async def list_unfinished(self, tenant_id: str | None = None) -> list[CodingJob]:
        return await asyncio.to_thread(self._list_unfinished, tenant_id)

    async def list_reapable(self, finished_before: str) -> list[CodingJob]:
        return await asyncio.to_thread(self._list_reapable, finished_before)


# ── Where a job may run ───────────────────────────────────────────────


def check_repo_path(raw: str) -> Path:
    """The resolved repository a job may work on, or ``ValueError`` saying why not.

    ``ROBOTHOR_CODING_REPO_ROOTS`` is required: with no roots no job starts. The
    path is resolved (symlinks included) before any check. Even under a root,
    the live workspace and the service user's home are refused — themselves and
    anything that contains them — because a job there could rewrite the engine's
    own code or reach every credential the home holds.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("repo_path is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("repo_path must be an absolute path")
    configured = str(_settings().repo_roots or "")
    roots = [
        Path(r.strip()).expanduser().resolve() for r in configured.split(os.pathsep) if r.strip()
    ]
    if not roots:
        raise ValueError(
            "coding jobs are disabled until ROBOTHOR_CODING_REPO_ROOTS names the directories "
            "repositories may live under (path-separated, for example ~/src)"
        )
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        shown = os.pathsep.join(str(r) for r in roots)
        raise ValueError(f"repo_path is outside ROBOTHOR_CODING_REPO_ROOTS ({shown})")
    workspace = sandbox.live_workspace()
    if workspace is not None and workspace.resolve().is_relative_to(resolved):
        raise ValueError(
            f"repo_path {resolved} is (or contains) the live workspace; coding jobs never work "
            "on the checkout the engine runs from"
        )
    with contextlib.suppress(RuntimeError):
        if Path.home().resolve().is_relative_to(resolved):
            raise ValueError(f"repo_path {resolved} is (or contains) the service user's home")
    return resolved


# ── Verification ──────────────────────────────────────────────────────


@dataclass
class VerifyOutcome:
    passed: bool
    exit_code: int | None
    output_tail: str
    output_sha256: str
    commits: list[str]
    reasons: list[str]
    pytest_ref: str = ""

    @property
    def commit_sha(self) -> str:
        return self.commits[0] if self.commits else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"commit_sha": self.commit_sha}


_PYTEST_PASSED = re.compile(r"\b(\d+) passed\b")
_PYTEST_FAILED = re.compile(r"\b(\d+) (?:failed|error|errors)\b")


def _pytest_ref(output: str) -> str:
    failed = _PYTEST_FAILED.findall(output)
    if failed:
        return f"pytest:failed:{sum(int(n) for n in failed)}"
    passed = _PYTEST_PASSED.findall(output)
    if passed:
        return f"pytest:passed:{int(passed[-1])}"
    return ""


async def _run_verify_command(
    argv: list[str], cwd: str, env: dict[str, str], timeout_s: float
) -> tuple[int, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return 127, f"verify command could not start: {exc}".encode()
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return 124, f"verify command timed out after {timeout_s:.0f}s".encode()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        raise
    return proc.returncode or 0, out


# ── Prompts ───────────────────────────────────────────────────────────


def _acceptance_text(job: CodingJob) -> str:
    if job.mode != "code":
        lines = ["This is a read-only job: do not modify, create or delete any file."]
        if job.json_schema is not None:
            lines.append("Return your answer as the requested structured output.")
        return "\n".join(lines)
    lines = [
        "## Acceptance",
        "The job is finished only when ALL of these hold; they are checked independently after you stop:",
    ]
    if job.acceptance.verify_command:
        lines.append(
            f"- `{job.acceptance.verify_command}` exits 0 when run from the repository root."
        )
    if job.acceptance.require_commit:
        lines.append(
            f"- Your work is committed on the current branch (`{job.branch}`): at least one new "
            "commit and a clean working tree."
        )
    lines.append("Do not push and do not switch branches.")
    return "\n".join(lines)


def _initial_prompt(job: CodingJob) -> str:
    return f"{job.task.strip()}\n\n{_acceptance_text(job)}"


def _resume_prompt(job: CodingJob) -> str:
    return (
        "The engine supervising you restarted while you were working, so your last turn was "
        "cut off. Check the state of the repository and continue the task where you left off.\n\n"
        f"The task:\n{job.task.strip()}\n\n{_acceptance_text(job)}"
    )


def _failure_prompt(job: CodingJob, outcome: VerifyOutcome) -> str:
    lines = [f"Acceptance failed (after round {job.rounds} of {job.max_rounds}):"]
    if job.acceptance.verify_command and outcome.exit_code not in (0, None):
        lines.append(
            f"- `{job.acceptance.verify_command}` finished with exit code {outcome.exit_code}. "
            "Output tail:"
        )
        lines.append("```\n" + outcome.output_tail[-_PROMPT_OUTPUT_TAIL:] + "\n```")
    lines.extend(f"- {reason}" for reason in outcome.reasons)
    lines.append("Fix it and commit. Do not push.")
    return "\n".join(lines)


def _followup_prompt(job: CodingJob, messages: list[str]) -> str:
    body = "\n\n".join(m.strip() for m in messages if m.strip())
    return f"Follow-up instructions from the orchestrator:\n{body}\n\n{_acceptance_text(job)}"


# ── The manager ───────────────────────────────────────────────────────


def _remove_config_dir(job_id: str) -> None:
    """Delete the job's private ``~/.config/robothor/claude-code/<id>``."""
    import shutil

    with contextlib.suppress(Exception):
        shutil.rmtree(coding_env.job_config_dir(job_id), ignore_errors=True)


Runner = Callable[..., Awaitable[ClaudeResult]]


def _default_github_token(agent_id: str, tenant_id: str) -> str | None:
    """GH_TOKEN for a job, only when the agent's OWN manifest grants it."""
    from robothor.engine.exec_env import grants_for_agent
    from robothor.secrets import resolve_secret

    grants = grants_for_agent(agent_id)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if name in grants:
            value = resolve_secret(name, tenant_id=tenant_id).value
            if value:
                return value
    return None


class CodingJobManager:
    """Owns every coding job's task in this engine process."""

    def __init__(
        self,
        *,
        store: JobStore,
        runner: Runner | None = None,
        token_resolver: Callable[[str], str | None] | None = None,
        github_token_resolver: Callable[[str, str], str | None] | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        self._store = store
        self._runner: Runner = runner or run_claude
        self._token_resolver = token_resolver or coding_env.resolve_oauth_token
        self._github_token_resolver = github_token_resolver or _default_github_token
        self._max_concurrent = max(1, max_concurrent or _settings().max_concurrent)
        self._live: dict[str, CodingJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._cancel_requested: set[str] = set()
        self._saves: set[asyncio.Task[None]] = set()
        self._save_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_reap = 0.0

    # ── public API ──

    async def start(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        task: str,
        repo_path: str,
        acceptance: Acceptance,
        mode: str = "code",
        model: str | None = None,
        max_budget_usd: float | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        base_ref: str = "HEAD",
        run_id: str = "",
        goal_id: str | None = None,
        task_id: str | None = None,
        grant_github: bool = False,
        json_schema: dict[str, Any] | None = None,
        effort: str | None = None,
        max_turns: int | None = None,
        round_timeout_s: float | None = None,
    ) -> CodingJob:
        """Create the worktree and the row, and start the job's task."""
        check_mode(mode)
        effort = check_effort(effort)
        if max_turns is not None and int(max_turns) < 1:
            raise ValueError("max_turns must be a positive integer")
        if round_timeout_s is not None and float(round_timeout_s) <= 0:
            raise ValueError("round_timeout_s must be a positive number of seconds")
        if not task.strip():
            raise ValueError("task is required")
        repo_path = str(check_repo_path(repo_path))
        if acceptance.verify_command:
            try:
                if not shlex.split(acceptance.verify_command):
                    raise ValueError
            except ValueError:
                raise ValueError("acceptance.verify_command could not be parsed") from None
        if mode == "code" and not acceptance.verify_command:
            raise ValueError(
                "code mode needs an acceptance verify_command: a command that exits 0 only "
                "when the task is done (for example `pytest -q tests/test_x.py`)"
            )
        if mode != "code":
            if acceptance.verify_command:
                # The acceptance command runs engine-side, outside Claude Code's
                # sandbox. In code mode it can do nothing the sandboxed session
                # could not; in a read-only mode it would be the only writer.
                raise ValueError(
                    f"acceptance.verify_command is only for code mode; a {mode} job is "
                    "read-only and is judged by its answer (use json_schema for structure)"
                )
            acceptance = Acceptance(
                verify_command=None,
                require_commit=False,
                second_pass=acceptance.second_pass,
                second_pass_below_turns=acceptance.second_pass_below_turns,
            )
        if grant_github and not await asyncio.to_thread(
            self._github_token_resolver, agent_id, tenant_id
        ):
            raise ValueError(
                "grant_github needs GH_TOKEN (or GITHUB_TOKEN) named in this agent's own "
                "manifest `secrets:` and stored in the vault"
            )

        job_id = str(uuid.uuid4())
        branch = f"genus/cc-{job_id[:8]}" if mode == "code" else None
        info = await wt.create_worktree(
            Path(repo_path),
            wt.worktree_path_for(job_id),
            branch=branch,
            base_ref=base_ref or "HEAD",
        )
        budget = max_budget_usd if max_budget_usd is not None else _settings().max_budget_usd
        job = CodingJob(
            id=job_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            task=task,
            repo_path=str(repo_path),
            mode=mode,
            acceptance=acceptance,
            worktree_path=str(info.path),
            branch=info.branch,
            base_ref=base_ref or "HEAD",
            base_sha=info.base_sha,
            model=model or _settings().claude_code_model.strip() or None,
            effort=effort,
            max_turns=int(max_turns) if max_turns is not None else None,
            round_timeout_s=float(round_timeout_s) if round_timeout_s is not None else None,
            max_rounds=max(1, min(int(max_rounds or DEFAULT_MAX_ROUNDS), MAX_MAX_ROUNDS)),
            max_budget_usd=float(budget) if budget else None,
            grant_github=grant_github,
            json_schema=json_schema,
            run_id=run_id,
            goal_id=goal_id,
            task_id=task_id,
        )
        try:
            await self._store.insert(job)
        except Exception:
            await wt.remove_worktree(Path(repo_path), info.path)
            raise
        self._spawn(job, _initial_prompt(job))
        return job

    async def get(
        self, job_id: str, tenant_id: str, *, agent_id: str | None = None
    ) -> CodingJob | None:
        """The job, when it is this tenant's — and, given ``agent_id``, that agent's.

        ``agent_id=None`` is the owner's (and the engine's own) view; an agent
        calling through a tool passes its id and sees only the jobs it started.
        """
        live = self._live.get(job_id)
        job = live if live is not None else await self._store.get(job_id, tenant_id)
        if job is None or job.tenant_id != tenant_id:
            return None
        if agent_id is not None and job.agent_id != agent_id:
            return None
        return job

    async def wait(
        self, job_id: str, tenant_id: str, timeout_s: float, *, agent_id: str | None = None
    ) -> CodingJob | None:
        """The job once it is finished, or as it stands when ``timeout_s`` runs out."""
        job = await self.get(job_id, tenant_id, agent_id=agent_id)
        if job is None:
            return None
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=max(0.0, float(timeout_s)))
        return await self.get(job_id, tenant_id, agent_id=agent_id)

    def _job_lock(self, job_id: str) -> asyncio.Lock:
        lock = self._locks.get(job_id)
        if lock is None:
            lock = self._locks[job_id] = asyncio.Lock()
        return lock

    async def followup(
        self, job_id: str, tenant_id: str, message: str, *, agent_id: str | None = None
    ) -> CodingJob:
        """Queue an instruction for a running job, or reopen a finished one with it.

        One lock per job makes "is it running? else reopen it" atomic: two
        follow-ups for a finished job reopen it once, and the second is queued
        on the reopened run. A follow-up that lands while the task is still
        finishing is queued, and the task picks it up before it exits.
        """
        if not message.strip():
            raise ValueError("message is required")
        async with self._job_lock(job_id):
            job = await self.get(job_id, tenant_id, agent_id=agent_id)
            if job is None:
                raise LookupError(job_id)
            if job.status == JobStatus.CANCELLED:
                raise ValueError("a cancelled job cannot be followed up; start a new job")
            if job_id in self._tasks and not self._tasks[job_id].done():
                job.pending_followups.append(message)
                await self._persist(job)
                return job
            if not job.session_id:
                raise ValueError("the job has no Claude Code session to follow up on yet")
            await self._reopen(job)
            self._spawn(job, _followup_prompt(job, [message]))
            return job

    async def _reopen(self, job: CodingJob) -> None:
        """Same session, same branch, a fresh allowance of rounds."""
        await self._ensure_worktree(job)
        job.status = JobStatus.QUEUED
        job.error = ""
        job.finished_at = None
        job.max_rounds = job.rounds + DEFAULT_MAX_ROUNDS
        await self._persist(job)

    async def cancel(
        self, job_id: str, tenant_id: str, *, agent_id: str | None = None
    ) -> CodingJob:
        job = await self.get(job_id, tenant_id, agent_id=agent_id)
        if job is None:
            raise LookupError(job_id)
        if job.is_terminal:
            return job
        self._cancel_requested.add(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        else:
            await self._finish(job, JobStatus.CANCELLED, "cancelled")
        return await self.get(job_id, tenant_id) or job

    async def resume_interrupted(self, tenant_id: str | None = None) -> int:
        """Continue every job a previous engine left ``queued`` or ``running``."""
        resumed = 0
        for job in await self._store.list_unfinished(tenant_id):
            if job.id in self._tasks:
                continue
            try:
                await self._ensure_worktree(job)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                await self._finish(
                    job, JobStatus.FAILED, f"could not restore worktree on resume: {exc}"
                )
                continue
            prompt = _resume_prompt(job) if job.session_id else _initial_prompt(job)
            self._spawn(job, prompt, resuming=bool(job.session_id))
            resumed += 1
        if resumed:
            logger.info("coding jobs: resumed %d interrupted job(s)", resumed)
        return resumed

    async def shutdown(self) -> None:
        """Stop every job task WITHOUT marking it, so the next engine resumes it."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        if self._saves:
            await asyncio.wait(set(self._saves))

    # ── internals ──

    def _sem(self, tenant_id: str) -> asyncio.Semaphore:
        sem = self._sems.get(tenant_id)
        if sem is None:
            sem = self._sems[tenant_id] = asyncio.Semaphore(self._max_concurrent)
        return sem

    def _spawn(self, job: CodingJob, prompt: str, *, resuming: bool = False) -> None:
        self._live[job.id] = job
        task = asyncio.create_task(
            self._drive(job, prompt, resuming), name=f"coding-job-{job.id[:8]}"
        )
        self._tasks[job.id] = task

        def _done(t: asyncio.Task[None], job_id: str = job.id) -> None:
            if self._tasks.get(job_id) is t:
                self._tasks.pop(job_id, None)
                self._live.pop(job_id, None)

        task.add_done_callback(_done)

    async def _persist(self, job: CodingJob) -> None:
        """Every write goes through one lock, so a later snapshot always lands last."""
        async with self._save_lock:
            await self._store.save(job)

    def _save_soon(self, job: CodingJob) -> None:
        save = asyncio.create_task(self._persist(job))
        self._saves.add(save)
        save.add_done_callback(self._saves.discard)

    def _on_event(self, job: CodingJob, event: ProgressEvent) -> None:
        job.events_tail = [*job.events_tail, event.line()][-_EVENTS_TAIL:]
        if event.session_id and event.session_id != job.session_id:
            # Persisted NOW: a crash before the round ends must still resume.
            job.session_id = event.session_id
            self._save_soon(job)

    async def _ensure_worktree(self, job: CodingJob) -> None:
        path = Path(job.worktree_path) if job.worktree_path else wt.worktree_path_for(job.id)
        if path.is_dir():
            return
        await wt.restore_worktree(Path(job.repo_path), path, job.branch, ref=job.base_sha)
        job.worktree_path = str(path)

    async def _job_paths(self, job: CodingJob) -> sandbox.JobPaths:
        common: Path | None = None
        admin: Path | None = None
        with contextlib.suppress(Exception):
            common, admin = await wt.git_dirs(job.worktree_path)
        return sandbox.JobPaths(
            home=Path.home(),
            workspace=sandbox.live_workspace(),
            worktree=Path(job.worktree_path),
            git_common_dir=common,
            git_admin_dir=admin,
            branch=job.branch,
        )

    async def _finish(self, job: CodingJob, status: JobStatus, error: str = "") -> None:
        job.status = status
        job.error = error
        job.finished_at = job.updated_at = _now()
        if status in (JobStatus.DONE, JobStatus.CANCELLED):
            # The branch keeps the commits; a failed job keeps its worktree and
            # its private config directory (the session transcript) so the
            # operator can see what was left behind — until the reaper's
            # retention runs out.
            if job.worktree_path:
                with contextlib.suppress(Exception):
                    await wt.remove_worktree(Path(job.repo_path), Path(job.worktree_path))
            _remove_config_dir(job.id)
        await self._persist(job)

    def _reap_soon(self) -> None:
        """Run the reaper in the background, at most once an hour per engine."""
        now = asyncio.get_running_loop().time()
        if self._last_reap and now - self._last_reap < _REAP_INTERVAL_S:
            return
        self._last_reap = now
        reap = asyncio.create_task(self._reap_quietly())
        self._saves.add(reap)
        reap.add_done_callback(self._saves.discard)

    async def _reap_quietly(self) -> None:
        try:
            await self.reap()
        except Exception:  # noqa: BLE001 - housekeeping never fails a job
            logger.warning("coding jobs: reaper failed", exc_info=True)

    async def reap(self, retention_days: int | None = None) -> int:
        """Remove what finished jobs older than the retention left behind.

        Their worktree, their private config directory and their
        ``genus/cc-*`` branch. The row stays, marked ``reaped_at``, as the
        record of what the job did. Returns how many jobs were reaped.
        """
        days = int(_settings().retention_days if retention_days is None else retention_days)
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        reaped = 0
        for job in await self._store.list_reapable(cutoff):
            if job.id in self._tasks:
                continue
            repo = Path(job.repo_path)
            path = Path(job.worktree_path) if job.worktree_path else wt.worktree_path_for(job.id)
            with contextlib.suppress(Exception):
                await wt.remove_worktree(repo, path)
            _remove_config_dir(job.id)
            if job.branch and repo.is_dir():
                with contextlib.suppress(Exception):
                    await wt.delete_job_branch(repo, job.branch)
            job.result["reaped_at"] = _now()
            await self._persist(job)
            reaped += 1
        if reaped:
            logger.info("coding jobs: reaped %d finished job(s) older than %d days", reaped, days)
        return reaped

    async def _git_identity(self, job: CodingJob) -> tuple[str, str] | None:
        name = _settings().git_name.strip()
        email = _settings().git_email.strip()
        if name and email:
            return name, email
        with contextlib.suppress(Exception):
            return await wt.git_identity(Path(job.repo_path))
        return None

    async def _verify(self, job: CodingJob, identity: tuple[str, str] | None) -> VerifyOutcome:
        reasons: list[str] = []
        exit_code: int | None = None
        output = b""
        if job.acceptance.verify_command:
            argv = shlex.split(job.acceptance.verify_command)
            env = coding_env.build_verify_env(job_id=job.id, git_identity=identity)
            exit_code, output = await _run_verify_command(
                argv,
                job.worktree_path,
                env,
                float(_settings().verify_timeout_s),
            )
        text = output.decode(errors="replace")
        commits: list[str] = []
        if job.mode == "code" and job.branch:
            commits = await wt.commits_on_branch(job.worktree_path, job.base_sha, job.branch)
            head = await wt.head_branch(job.worktree_path)
            if head != job.branch:
                where = f"on branch `{head}`" if head else "detached"
                reasons.append(
                    f"HEAD is {where}, not on the job branch `{job.branch}`: check out "
                    f"`{job.branch}` and commit your work there (never switch branches)."
                )
            if job.acceptance.require_commit:
                if not commits:
                    reasons.append("There is no new commit on the job branch: commit your work.")
                dirty = await wt.dirty_paths(job.worktree_path)
                if dirty:
                    shown = ", ".join(dirty[:10]) + (
                        f" (+{len(dirty) - 10} more)" if len(dirty) > 10 else ""
                    )
                    reasons.append(
                        f"The working tree has uncommitted changes ({shown}): commit them (or "
                        "discard what does not belong) so the committed state is what passes."
                    )
        passed = (exit_code == 0 if job.acceptance.verify_command else True) and not reasons
        return VerifyOutcome(
            passed=passed,
            exit_code=exit_code,
            output_tail=text[-_OUTPUT_TAIL:],
            output_sha256=hashlib.sha256(output).hexdigest(),
            commits=commits,
            reasons=reasons,
            pytest_ref=_pytest_ref(text),
        )

    def _record_evidence(self, job: CodingJob, outcome: VerifyOutcome) -> None:
        from robothor.engine.session_goal import GoalEvidence

        evidence: list[dict[str, Any]] = []
        if job.acceptance.verify_command:
            evidence.append(
                GoalEvidence(
                    kind="test_run",
                    summary=(
                        f"claude_code job {job.id}: `{job.acceptance.verify_command}` exited "
                        f"{outcome.exit_code} (output sha256 {outcome.output_sha256})"
                    ),
                    # A pytest summary when the output has one; otherwise the
                    # job's own UUID, which names the row holding the record.
                    reference=outcome.pytest_ref
                    if outcome.pytest_ref.startswith("pytest:passed")
                    else job.id,
                ).to_dict()
            )
        if outcome.commit_sha:
            evidence.append(
                GoalEvidence(
                    kind="commit",
                    summary=f"claude_code job {job.id}: {len(outcome.commits)} commit(s) on {job.branch}",
                    reference=outcome.commit_sha,
                ).to_dict()
            )
        job.result["evidence"] = evidence

    @staticmethod
    def _second_pass_prompt(job: CodingJob, result: ClaudeResult) -> str:
        """The second-pass prompt when this finished round earns one, else "" (recorded)."""
        instruction = job.acceptance.second_pass
        if not instruction or job.mode == "code" or "second_pass" in job.result:
            return ""
        below = job.acceptance.second_pass_below_turns
        if job.rounds >= job.max_rounds or (
            below is not None and int(result.num_turns or 0) >= below
        ):
            job.result["second_pass"] = "skipped"
            return ""
        job.result["second_pass"] = "running"
        return _followup_prompt(job, [instruction])

    async def _drive(self, job: CodingJob, prompt: str, resuming: bool) -> None:
        try:
            while True:
                async with self._sem(job.tenant_id):
                    await self._loop(job, prompt, resuming)
                # A follow-up queued while the job was finishing (its task was
                # still alive, so it was queued rather than reopening the job)
                # is run here. No await between this check and the return, so
                # nothing can be queued after it and lost.
                if (
                    job.status == JobStatus.CANCELLED
                    or not job.pending_followups
                    or not job.session_id
                ):
                    break
                messages, job.pending_followups = job.pending_followups, []
                await self._reopen(job)
                prompt, resuming = _followup_prompt(job, messages), False
        except asyncio.CancelledError:
            if job.id in self._cancel_requested:
                self._cancel_requested.discard(job.id)
                await self._finish(job, JobStatus.CANCELLED, "cancelled by the orchestrator")
                return
            # Engine shutdown: leave the row as it is, for resume.
            raise
        except Exception as exc:  # noqa: BLE001 - the job fails, the engine does not
            logger.exception("coding job %s crashed", job.id)
            await self._finish(
                job, JobStatus.FAILED, f"internal error: {type(exc).__name__}: {exc}"
            )
        if job.is_terminal:
            self._reap_soon()

    async def _loop(self, job: CodingJob, prompt: str, resuming: bool) -> None:
        job.status = JobStatus.RUNNING
        job.updated_at = _now()
        await self._persist(job)

        token = await asyncio.to_thread(self._token_resolver, job.tenant_id)
        if coding_env.uses_host_login(token) and not coding_env.host_login_present():
            await self._finish(
                job,
                JobStatus.FAILED,
                "No Claude Code credential: the engine's user is not logged in to Claude Code "
                "on this host (run `claude` once and sign in), and no token is stored "
                f"(`robothor claude-code login` stores {coding_env.TOKEN_ENV} in the vault).",
            )
            return
        github = (
            await asyncio.to_thread(self._github_token_resolver, job.agent_id, job.tenant_id)
            if job.grant_github
            else None
        )
        identity = await self._git_identity(job)
        env = coding_env.build_claude_env(
            job_id=job.id, oauth_token=token, github_token=github, git_identity=identity
        )
        paths = await self._job_paths(job)
        allowed_tools, disallowed_tools = sandbox.build_permissions(job.mode, paths)
        settings = sandbox.build_sandbox_settings(
            job.mode, paths, allowed_domains=sandbox.allowed_domains()
        )
        round_timeout = float(job.round_timeout_s or _settings().round_timeout_s)
        max_turns = int(job.max_turns or _settings().max_turns)
        needs_verify = job.mode == "code" or bool(job.acceptance.verify_command)

        if resuming and needs_verify and job.base_sha:
            # It may have finished just before the engine went down.
            outcome = await self._verify(job, identity)
            if outcome.passed:
                job.result["verify"] = outcome.to_dict()
                job.result["commit_sha"] = outcome.commit_sha
                job.result["commits"] = outcome.commits
                self._record_evidence(job, outcome)
                await self._finish(job, JobStatus.DONE)
                return

        while True:
            if job.rounds >= job.max_rounds:
                last = job.result.get("verify") or {}
                detail = "; ".join(last.get("reasons") or [])
                if last.get("exit_code") not in (None, 0):
                    detail = f"verify exit code {last.get('exit_code')}" + (
                        f"; {detail}" if detail else ""
                    )
                await self._finish(
                    job,
                    JobStatus.FAILED,
                    f"acceptance not met after {job.rounds} round(s)"
                    + (f": {detail}" if detail else ""),
                )
                return
            remaining = None
            if job.max_budget_usd is not None:
                remaining = round(job.max_budget_usd - job.cost_usd, 4)
                if remaining <= 0:
                    await self._finish(
                        job, JobStatus.FAILED, f"budget of ${job.max_budget_usd:.2f} exhausted"
                    )
                    return

            job.rounds += 1
            job.updated_at = _now()
            await self._persist(job)
            inv = ClaudeInvocation(
                prompt=prompt,
                cwd=job.worktree_path,
                model=job.model,
                effort=job.effort,
                allowed_tools=allowed_tools,
                disallowed_tools=disallowed_tools,
                max_turns=max_turns,
                max_budget_usd=remaining,
                append_system_prompt=SYSTEM_PROMPT,
                json_schema=job.json_schema,
                resume_session_id=job.session_id,
                settings=settings,
            )
            result = await self._runner(
                inv, env=env, timeout_s=round_timeout, on_event=lambda e: self._on_event(job, e)
            )
            job.session_id = result.session_id or job.session_id
            spent = float(result.total_cost_usd or 0.0)
            if not result.got_result and remaining is not None:
                # Killed or crashed before Claude Code reported its cost: the
                # round may have spent all it was allowed, so it is charged that.
                spent = max(spent, remaining)
            job.cost_usd = round(job.cost_usd + spent, 6)
            job.turns += int(result.num_turns or 0)
            job.result["last_round"] = {
                "round": job.rounds,
                "charged_usd": spent,
                "is_error": result.is_error,
                "subtype": result.subtype,
                "error": result.error_summary,
                "cost_usd": result.total_cost_usd,
                "turns": result.num_turns,
                "permission_denials": len(result.permission_denials),
            }
            job.result["claude_result"] = result.result_text[-_OUTPUT_TAIL:]
            if result.model:
                # What the alias resolved to this round: the review footer cites it.
                job.result["model"] = result.model
                job.result["models_used"] = list(result.models_used)
            if result.structured_output is not None:
                job.result["structured_output"] = result.structured_output
            job.updated_at = _now()
            await self._persist(job)

            if result.is_error and not job.session_id:
                await self._finish(job, JobStatus.FAILED, f"claude failed: {result.error_summary}")
                return

            if job.pending_followups:
                messages, job.pending_followups = job.pending_followups, []
                prompt = _followup_prompt(job, messages)
                continue

            if not needs_verify:
                missing_structured = (
                    job.json_schema is not None and result.structured_output is None
                )
                if job.result.get("second_pass") == "running":
                    # The answer is the second round's when it gave one, else
                    # the first round's (still in structured_output).
                    ok = not result.is_error and not missing_structured
                    job.result["second_pass"] = "done" if ok else "incomplete"
                    await self._finish(job, JobStatus.DONE)
                    return
                if not result.is_error and not missing_structured:
                    second = self._second_pass_prompt(job, result)
                    if second:
                        prompt = second
                        continue
                    await self._finish(job, JobStatus.DONE)
                    return
                prompt = (
                    "Your previous attempt did not finish"
                    + (f" ({result.error_summary})" if result.is_error else "")
                    + (": the structured output is missing." if missing_structured else ".")
                    + " Finish the task."
                )
            else:
                outcome = await self._verify(job, identity)
                job.result["verify"] = outcome.to_dict()
                job.result["commit_sha"] = outcome.commit_sha
                job.result["commits"] = outcome.commits
                if outcome.passed:
                    self._record_evidence(job, outcome)
                    await self._finish(job, JobStatus.DONE)
                    return
                await self._persist(job)
                prompt = _failure_prompt(job, outcome)

            if job.max_budget_usd is not None and job.cost_usd >= job.max_budget_usd:
                await self._finish(
                    job,
                    JobStatus.FAILED,
                    f"budget of ${job.max_budget_usd:.2f} exhausted before acceptance was met",
                )
                return


# ── Process-wide manager ──────────────────────────────────────────────

_manager: CodingJobManager | None = None


def get_manager() -> CodingJobManager:
    """The engine's manager, backed by Postgres."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        _manager = CodingJobManager(store=PgJobStore())
    return _manager


def use_manager(manager: CodingJobManager | None) -> None:
    """Install a manager (tests), or ``None`` to rebuild the default lazily."""
    global _manager  # noqa: PLW0603
    _manager = manager


async def resume_interrupted_jobs(tenant_id: str | None = None) -> int:
    """Engine-startup hook: continue jobs a previous engine left unfinished."""
    return await get_manager().resume_interrupted(tenant_id)
