"""One CRM task per pull-request head: what the intake hands the review worker.

The task body is ``key: value`` lines ``pr_review_work`` reads; the
``prReview: <repo>#<n>@<sha>`` line is the dedupe marker
(:func:`robothor.crm.dal.build_dedup_marker`), so a second intake tick for the
same head finds the open task instead of filing another. ``requested_at`` is
when the request was made, where ``pr_review_status`` starts its latency.
:class:`CrmTaskQueue` is the worker's side: the open tasks, oldest first.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from robothor.pr_review.store import PrReviewRow

__all__ = [
    "DEDUP_KEY",
    "TASK_TAG",
    "CrmTaskQueue",
    "CrmTaskSink",
    "ReviewTaskSpec",
    "TaskSink",
    "task_body",
]

DEDUP_KEY = "prReview"
TASK_TAG = "pr-review"


@dataclass(frozen=True)
class ReviewTaskSpec:
    row: PrReviewRow
    head_sha: str
    mode: str
    since_sha: str
    depth: str
    trigger: str
    agent_id: str

    @property
    def dedup_value(self) -> str:
        return f"{self.row.repo}#{self.row.number}@{self.head_sha}"

    @property
    def title(self) -> str:
        return f"pr-review {self.row.repo}#{self.row.number} @{self.head_sha[:7]}"


def task_body(spec: ReviewTaskSpec) -> str:
    from robothor.crm.dal import build_dedup_marker

    row = spec.row
    lines = [
        f"Review pull request {row.repo}#{row.number} at {spec.head_sha}.",
        "",
        f"repo: {row.repo}",
        f"number: {row.number}",
        f"url: {row.url or f'https://github.com/{row.repo}/pull/{row.number}'}",
        f"head_sha: {spec.head_sha}",
        f"last_reviewed_sha: {row.last_reviewed_sha or 'none'}",
        f"mode: {spec.mode}",
        f"since_sha: {spec.since_sha or 'none'}",
        f"depth: {spec.depth}",
        f"trigger: {spec.trigger}",
    ]
    if row.chat_thread:
        lines += [
            f"chat_space: {row.chat_space}",
            f"chat_thread: {row.chat_thread}",
            f"chat_message: {row.chat_message}",
        ]
    if row.requested_at:
        lines.append(f"requested_at: {row.requested_at.isoformat()}")
    lines += [
        "",
        build_dedup_marker(DEDUP_KEY, spec.dedup_value),
        "",
        "Reviewed by pr_review_work: pr_review_prepare(repo, number) -> wait for the job -> "
        "pr_review_finalize(repo, number, job_id) -> resolved with the review URL.",
    ]
    return "\n".join(lines)


class TaskSink(Protocol):
    async def create(self, spec: ReviewTaskSpec) -> str: ...


class CrmTaskSink:
    """Files the task through the CRM DAL, reusing an open task for the same head."""

    def __init__(self, tenant_id: str, created_by: str = "pr-review-intake") -> None:
        self.tenant_id = tenant_id
        self.created_by = created_by

    def _create(self, spec: ReviewTaskSpec) -> str:
        from robothor.crm.dal import create_task, find_task_by_dedup_key

        existing = find_task_by_dedup_key(
            DEDUP_KEY,
            spec.dedup_value,
            include_recently_resolved=False,
            assigned_to_agent=spec.agent_id,
            tenant_id=self.tenant_id,
        )
        if existing:
            return str(existing["id"])
        created = create_task(
            title=spec.title,
            body=task_body(spec),
            created_by_agent=self.created_by,
            assigned_to_agent=spec.agent_id,
            tags=[TASK_TAG],
            tenant_id=self.tenant_id,
        )
        if not created or isinstance(created, dict):
            raise RuntimeError(f"CRM task not created: {created}")
        return str(created)

    async def create(self, spec: ReviewTaskSpec) -> str:
        return await asyncio.to_thread(self._create, spec)

    def _reopen(self, task_id: str) -> None:
        from robothor.crm.dal import get_task, update_task

        task = get_task(task_id, tenant_id=self.tenant_id)
        if not task or str(task.get("status") or "") != "IN_PROGRESS":
            return
        update_task(task_id, changed_by=self.created_by, tenant_id=self.tenant_id, status="TODO")

    def _close(self, task_id: str, resolution: str) -> None:
        from robothor.crm.dal import resolve_task

        resolve_task(task_id, resolution, agent_id=self.created_by, tenant_id=self.tenant_id)

    async def close(self, task_id: str, resolution: str) -> None:
        """Resolve a review task the operator stopped."""
        await asyncio.to_thread(self._close, task_id, resolution)

    async def reopen(self, task_id: str) -> None:
        """Put an orphaned review's IN_PROGRESS task back to TODO."""
        await asyncio.to_thread(self._reopen, task_id)


class CrmTaskQueue:
    """The review worker's queue: the agent's open ``pr-review`` tasks, oldest first.

    Resolved as the reviewer agent (``cfg.agent_id``), the identity the tasks
    are assigned to, so their history reads as it did when the agent ran them.
    """

    def __init__(self, tenant_id: str, agent_id: str) -> None:
        self.tenant_id = tenant_id
        self.agent_id = agent_id

    def _list_open(self) -> list[dict[str, Any]]:
        from robothor.crm.dal import list_tasks

        found = list_tasks(
            assigned_to_agent=self.agent_id,
            tags=[TASK_TAG],
            limit=200,
            tenant_id=self.tenant_id,
        )
        return sorted(found, key=lambda t: str(t.get("createdAt") or ""))

    async def list_open(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_open)

    def _start(self, task_id: str) -> None:
        from robothor.crm.dal import update_task

        update_task(
            task_id, changed_by=self.agent_id, tenant_id=self.tenant_id, status="IN_PROGRESS"
        )

    async def start(self, task_id: str) -> None:
        await asyncio.to_thread(self._start, task_id)

    def _resolve(self, task_id: str, resolution: str) -> None:
        from robothor.crm.dal import resolve_task

        resolve_task(task_id, resolution, agent_id=self.agent_id, tenant_id=self.tenant_id)

    async def resolve(self, task_id: str, resolution: str) -> None:
        await asyncio.to_thread(self._resolve, task_id, resolution)
