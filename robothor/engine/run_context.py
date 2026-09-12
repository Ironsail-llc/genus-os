"""Is the code running right now inside a benchmark run, and may it write?

Incident 2026-09-12. The benchmark harness executes each task as a child run of
the agent it is grading. With the sandbox ``off`` that child runs under the
instance's own tenant, and "off means read-only" was an assumption nobody
enforced below the tool layer: a suite fixture's fictional person became
durable ``memory_facts`` rows, which a real agent later read back as
established fact and turned into CRM people and tasks the operator saw.

The tool deny-set is a good control and it is not the boundary. It is computed
once per suite from the agent's ``tools_allowed``, it cannot see a write that
reaches the DAL without passing a tool, and it has been re-opened by accident
twice. The boundary is the write itself.

Why a contextvar rather than a parameter
----------------------------------------
``store_facts_batch`` has around thirty callers across ingestion, consolidation
and the tools. Threading a "this is a benchmark" argument through all of them
would put the decision back at the call sites — the exact shape of the defect,
one clause per path, each able to be forgotten. A contextvar set for the
duration of the run answers the question at the bottom instead, for every
caller that exists now and every one added later.

Task-locality is what makes this safe: an ``asyncio`` task inherits a COPY of
the context it was spawned from, so a benchmark child's flag cannot escape
sideways into a concurrent production run. It can only escape forward, into the
same task after the child returns, which is what :func:`benchmark_run_scope`
exists to prevent.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "BenchmarkRun",
    "benchmark_run_scope",
    "benchmark_write_refused",
    "current_benchmark_run",
    "in_benchmark_run",
    "mark_benchmark_run",
    "set_benchmark_run",
]


@dataclass(frozen=True)
class BenchmarkRun:
    """Who is being graded, and under which run. Identifiers only.

    Deliberately carries no prompt, no content and no fixture text: this object
    exists to be named in a WARNING line, and the whole reason that WARNING is
    worth writing is that the content it refers to is untrusted benchmark
    fiction that must not be copied anywhere durable.
    """

    agent_id: str = ""
    run_id: str = ""


_CURRENT: ContextVar[BenchmarkRun | None] = ContextVar("genus_benchmark_run", default=None)


def set_benchmark_run(run: BenchmarkRun | None) -> None:
    """Mark (or unmark) the current task as executing a benchmark run.

    Used by the runner, which has no single exit point to restore a token from.
    Prefer :func:`benchmark_run_scope` anywhere a ``with`` block is possible.
    """
    _CURRENT.set(run)


@contextmanager
def benchmark_run_scope(
    is_benchmark: bool, *, agent_id: str = "", run_id: str = ""
) -> Iterator[None]:
    """Bind the benchmark marker for the block, and restore it on the way out.

    ``is_benchmark=False`` binds ``None`` rather than doing nothing, so a nested
    non-benchmark run inside a benchmark one is not treated as benchmark
    traffic. The restore is unconditional: the benchmark RUNNER is an ordinary
    agent whose own writes (``benchmark_results``, its report) must still work
    the moment the child it was grading returns.
    """
    token = _CURRENT.set(BenchmarkRun(agent_id=agent_id, run_id=run_id) if is_benchmark else None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def mark_benchmark_run(session: Any, agent_config: Any, agent_id: str) -> None:
    """Stamp one run's benchmark marker onto the run row AND this task.

    Called once per run by ``AgentRunner.execute``. Lives here rather than in
    the runner because the runner is the god-object this platform keeps
    decomposing, and because the two halves of the marker belong together: the
    row is what the tool wrappers read, the context is what the write boundary
    reads, and a run where those two disagree is the bug.

    Set, not scoped: ``execute`` has a dozen exit points and no single
    ``finally`` to restore a token from. The scope is the CALLER's —
    ``tools/handlers/benchmark.py::_execute_task_run`` wraps every harness child
    in :func:`benchmark_run_scope`, which restores the previous marker on the
    way out so the benchmark runner's own writes are unaffected. Stamping
    unconditionally (``None`` for an ordinary run) means a nested production run
    cannot inherit a benchmark marker either.
    """
    session.run.is_benchmark = bool(getattr(agent_config, "is_benchmark", False))
    set_benchmark_run(
        BenchmarkRun(agent_id=agent_id, run_id=str(getattr(session, "run_id", "") or ""))
        if session.run.is_benchmark
        else None
    )


def current_benchmark_run() -> BenchmarkRun | None:
    """The benchmark run this task is inside, or None."""
    return _CURRENT.get()


def in_benchmark_run() -> bool:
    """True when this task is executing a benchmark run."""
    return _CURRENT.get() is not None


def benchmark_write_refused(tenant_id: str, *, what: str) -> bool:
    """True when the current benchmark run must not write ``tenant_id``.

    The rule is one line and has no exceptions: a benchmark run may write only
    the dedicated sandbox tenant, whose rows are invisible to production reads
    and are deleted when the task ends. Everything else — including the
    instance's own tenant, including an empty tenant that would resolve to the
    default — is refused.

    Args:
        tenant_id: tenant the write is addressed to.
        what: short, content-free name of the write, for the log line.

    Returns:
        True when the caller must not proceed. Logs one WARNING when it does,
        naming the agent, the run and the tenant, and never the content.
    """
    current = _CURRENT.get()
    if current is None:
        return False

    from robothor.engine.benchmark_sandbox import sandbox_tenant_id

    if (tenant_id or "").strip() == sandbox_tenant_id():
        return False

    logger.warning(
        "benchmark isolation: refused %s for agent=%s run=%s tenant=%s "
        "(a benchmark run may only write the sandbox tenant)",
        what,
        current.agent_id or "<unknown>",
        current.run_id or "<unknown>",
        tenant_id or "<unset>",
    )
    return True
