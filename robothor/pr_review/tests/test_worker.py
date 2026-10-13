"""pr_review_work's loop: the open review tasks, several at once, no model.

It replaced the reviewer agent, which took one task at a time and woke at most
every two minutes: 18 pull requests posted together took 1 h 45 min. The
per-task steps are the agent brief's, unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from robothor.pr_review.config import ReviewerConfig
from robothor.pr_review.tests.fakes import REPO, FakeQueue, review_task
from robothor.pr_review.worker import parse_task, work

URL = f"https://github.com/{REPO}/pull/"


class Reviews:
    """prepare / wait / finalize over fake jobs that finish when told to."""

    def __init__(self, *, auto_finish: bool = True) -> None:
        self.auto_finish = auto_finish
        self.done: dict[str, asyncio.Event] = {}
        self.running: set[str] = set()
        self.peak = 0
        self.prepared: list[int] = []
        self.waited: list[str] = []
        self.finalized: list[tuple[str, int, str]] = []
        self.prepare_result: dict[int, dict[str, Any]] = {}
        self.finalize_result: dict[int, dict[str, Any]] = {}

    def finish(self, number: int) -> None:
        self.done.setdefault(f"job-{number}", asyncio.Event()).set()

    async def prepare(self, repo: str, number: int) -> dict[str, Any]:
        self.prepared.append(number)
        if number in self.prepare_result:
            return self.prepare_result[number]
        job = f"job-{number}"
        self.done.setdefault(job, asyncio.Event())
        self.running.add(job)
        self.peak = max(self.peak, len(self.running))
        return {
            "repo": repo,
            "number": number,
            "head_sha": f"{number:07d}" + "0" * 33,
            "job_id": job,
        }

    async def wait(self, job_id: str, timeout: float) -> str:
        self.waited.append(job_id)
        event = self.done.setdefault(job_id, asyncio.Event())
        if self.auto_finish:
            await asyncio.sleep(0.01)
            event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return "running"
        self.running.discard(job_id)
        return "done"

    async def finalize(self, repo: str, number: int, job_id: str) -> dict[str, Any]:
        self.finalized.append((repo, number, job_id))
        if number in self.finalize_result:
            return self.finalize_result[number]
        return {
            "status": "posted",
            "verdict": "APPROVE",
            "review_url": f"{URL}{number}#pullrequestreview-1",
            "digest": "",
        }


async def _work(queue, reviews, cfg=None, **kw):
    kw.setdefault("poll_s", 0.01)
    return await work(
        cfg or ReviewerConfig(workers=3),
        queue,
        prepare=reviews.prepare,
        wait=reviews.wait,
        finalize=reviews.finalize,
        **kw,
    )


def test_parse_task_reads_repo_and_number_from_the_body():
    assert parse_task(review_task("t", 12)) == (REPO, 12)
    assert parse_task({"id": "t", "body": "nothing here"}) is None


async def test_five_tasks_three_workers_never_more_than_three_jobs():
    queue = FakeQueue([review_task(f"t{n}", n) for n in range(1, 6)])
    reviews = Reviews()
    result = await _work(queue, reviews)
    assert reviews.peak == 3
    assert reviews.prepared == [1, 2, 3, 4, 5]  # oldest first
    assert queue.started == ["t1", "t2", "t3", "t4", "t5"]
    assert queue.resolved == {
        f"t{n}": f"APPROVE: {URL}{n}#pullrequestreview-1" for n in range(1, 6)
    }
    assert result["processed"] == 5 and result["unfinished"] == 0


async def test_a_task_filed_mid_run_starts_when_a_slot_frees():
    queue = FakeQueue([review_task("t1", 1)])
    reviews = Reviews(auto_finish=False)

    async def later():
        await asyncio.sleep(0.05)
        queue.tasks.append(review_task("t2", 2))
        await asyncio.sleep(0.05)
        reviews.finish(1)
        await asyncio.sleep(0.05)
        reviews.finish(2)

    helper = asyncio.create_task(later())
    result = await _work(queue, reviews)
    await helper
    assert reviews.prepared == [1, 2]
    assert set(queue.resolved) == {"t1", "t2"} and result["processed"] == 2


async def test_a_failing_prepare_resolves_its_task_and_stalls_nothing():
    queue = FakeQueue([review_task(f"t{n}", n) for n in range(1, 4)])
    reviews = Reviews()
    reviews.prepare_result[1] = {"error": "checkout failed: boom"}
    reviews.prepare_result[2] = {"skip": True, "reason": "pull request is closed; resolve the task"}
    await _work(queue, reviews)
    assert queue.resolved["t1"] == "Prepare failed: checkout failed: boom"
    assert queue.resolved["t2"] == "pull request is closed; resolve the task"
    assert queue.resolved["t3"].startswith("APPROVE: ")
    assert [f[1] for f in reviews.finalized] == [3]


async def test_a_prepare_that_raises_resolves_its_task_too():
    queue = FakeQueue([review_task("t1", 1), review_task("t2", 2)])
    reviews = Reviews()

    async def boom(repo: str, number: int) -> dict[str, Any]:
        if number == 1:
            raise RuntimeError("database gone")
        return await Reviews.prepare(reviews, repo, number)

    reviews.prepare = boom  # type: ignore[method-assign]
    await _work(queue, reviews)
    assert queue.resolved["t1"] == "Prepare failed: RuntimeError: database gone"
    assert queue.resolved["t2"].startswith("APPROVE: ")


async def test_a_resumed_review_skips_the_wait_and_an_already_started_one_waits():
    queue = FakeQueue([review_task("t1", 1), review_task("t2", 2)])
    reviews = Reviews()
    reviews.prepare_result[1] = {"job_id": "job-old", "resumed": True, "head_sha": "a" * 40}
    reviews.prepare_result[2] = {"job_id": "job-2", "already_started": True, "head_sha": "b" * 40}
    await _work(queue, reviews)
    assert reviews.waited == ["job-2"]
    assert sorted(reviews.finalized) == [(REPO, 1, "job-old"), (REPO, 2, "job-2")]


async def test_a_failed_review_resolves_with_its_status_and_error():
    queue = FakeQueue([review_task("t1", 1), review_task("t2", 2), review_task("t3", 3)])
    reviews = Reviews()
    reviews.finalize_result[1] = {"status": "failed", "error": "review job failed: x", "digest": ""}
    reviews.finalize_result[2] = {"error": "coding job not found for this tenant"}
    reviews.finalize_result[3] = {"status": "stale", "next": "resolve the task: the head moved"}
    await _work(queue, reviews)
    assert queue.resolved["t1"] == "failed: review job failed: x"
    assert queue.resolved["t2"] == "Finalize failed: coding job not found for this tenant"
    assert queue.resolved["t3"] == "stale: resolve the task: the head moved"


async def test_digest_lines_are_sent_as_they_come_and_returned():
    queue = FakeQueue([review_task("t1", 1)])
    reviews = Reviews()
    line = f"PR review {REPO}#1: Comments/change request — {URL}1#r"
    reviews.finalize_result[1] = {
        "status": "posted",
        "verdict": "REQUEST_CHANGES",
        "review_url": f"{URL}1#r",
        "digest": line,
    }
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    result = await _work(queue, reviews, notify=notify)
    assert sent == [line]
    assert result["digest"] == [line]


async def test_a_failing_notify_never_fails_the_review():
    queue = FakeQueue([review_task("t1", 1)])
    reviews = Reviews()
    reviews.finalize_result[1] = {
        "status": "posted",
        "verdict": "APPROVE",
        "review_url": "u",
        "digest": "PR review x",
    }

    async def notify(text: str) -> None:
        raise RuntimeError("telegram down")

    await _work(queue, reviews, notify=notify)
    assert queue.resolved["t1"] == "APPROVE: u"


async def test_no_new_task_after_the_take_window_and_unfinished_work_is_left_open():
    queue = FakeQueue([review_task("t1", 1), review_task("t2", 2)])
    reviews = Reviews(auto_finish=False)
    result = await _work(
        queue, reviews, cfg=ReviewerConfig(workers=1), take_for_s=0.02, deadline_s=0.1
    )
    # t1 started and never finished: left IN_PROGRESS for the next run to pick
    # up (prepare re-binds its job); t2 was never taken.
    assert reviews.prepared == [1]
    assert queue.resolved == {}
    assert result["unfinished"] == 1
    assert result["outcomes"][0]["status"] == "unfinished"


async def test_an_in_progress_task_from_a_dead_run_is_taken_again():
    queue = FakeQueue([review_task("t1", 1, status="IN_PROGRESS")])
    reviews = Reviews()
    await _work(queue, reviews)
    assert "t1" in queue.resolved
    assert queue.started == []  # already IN_PROGRESS: no transition to make


async def test_the_status_file_lists_what_the_run_did(tmp_path):
    path = tmp_path / "memory" / "pr-reviewer-status.md"
    queue = FakeQueue([review_task("t1", 1)])
    reviews = Reviews()
    await _work(queue, reviews, cfg=ReviewerConfig(workers=3, status_file=str(path)))
    text = path.read_text()
    assert text.startswith("# PR Reviewer Status\nLast run: ")
    assert f"- {REPO}#1 @0000001: APPROVE {URL}1#pullrequestreview-1" in text


async def test_an_idle_run_returns_at_once_and_writes_nothing(tmp_path):
    path = tmp_path / "status.md"
    result = await _work(
        FakeQueue(), Reviews(), cfg=ReviewerConfig(workers=3, status_file=str(path))
    )
    assert result["processed"] == 0 and not path.exists()


async def test_an_unreadable_task_is_resolved_not_retried_forever():
    queue = FakeQueue([{"id": "t9", "status": "TODO", "body": "no repo here"}])
    await _work(queue, Reviews())
    assert queue.resolved["t9"].startswith("Not a review task")
    assert queue.started == ["t9"]  # the CRM resolves a task only from IN_PROGRESS


async def test_cancelling_the_run_cancels_its_waits():
    queue = FakeQueue([review_task("t1", 1)])
    reviews = Reviews(auto_finish=False)
    run = asyncio.create_task(_work(queue, reviews))
    await asyncio.sleep(0.05)
    run.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run
    await asyncio.sleep(0.01)
    assert [t for t in asyncio.all_tasks() if "one" in repr(t.get_coro())] == []
    assert queue.resolved == {}


async def test_the_crm_queue_lists_the_agents_open_review_tasks_oldest_first(monkeypatch):
    from robothor.crm import dal
    from robothor.pr_review.tasks import TASK_TAG, CrmTaskQueue

    calls: dict[str, Any] = {}

    def fake_list(**kw):
        calls["list"] = kw
        return [
            {"id": "new", "createdAt": "2026-10-13T11:05:00+00:00"},
            {"id": "old", "createdAt": "2026-10-13T11:00:00+00:00"},
        ]

    monkeypatch.setattr(dal, "list_tasks", fake_list)
    monkeypatch.setattr(dal, "update_task", lambda *a, **kw: calls.setdefault("update", (a, kw)))
    monkeypatch.setattr(dal, "resolve_task", lambda *a, **kw: calls.setdefault("resolve", (a, kw)))
    queue = CrmTaskQueue("tenant-a", "pr-reviewer")
    assert [t["id"] for t in await queue.list_open()] == ["old", "new"]
    assert calls["list"]["assigned_to_agent"] == "pr-reviewer"
    assert calls["list"]["tags"] == [TASK_TAG] and calls["list"]["tenant_id"] == "tenant-a"
    await queue.start("old")
    assert calls["update"][1]["status"] == "IN_PROGRESS"
    await queue.resolve("old", "APPROVE: u")
    assert calls["resolve"][0] == ("old", "APPROVE: u")
    assert calls["resolve"][1]["agent_id"] == "pr-reviewer"


async def test_the_crm_queue_raises_when_the_crm_refuses(monkeypatch):
    from robothor.crm import dal
    from robothor.pr_review.tasks import CrmTaskQueue

    monkeypatch.setattr(dal, "update_task", lambda *a, **kw: {"error": "bad transition"})
    monkeypatch.setattr(dal, "resolve_task", lambda *a, **kw: {"error": "Cannot resolve"})
    queue = CrmTaskQueue("tenant-a", "pr-reviewer")
    with pytest.raises(RuntimeError, match="bad transition"):
        await queue.start("t1")
    with pytest.raises(RuntimeError, match="Cannot resolve"):
        await queue.resolve("t1", "APPROVE: u")
    monkeypatch.setattr(dal, "resolve_task", lambda *a, **kw: False)
    with pytest.raises(RuntimeError, match="not found"):
        await queue.resolve("t1", "APPROVE: u")


# ── A job this engine is not running ───────────────────────────────────
# CodingJobManager.wait returns at once, still running, when this engine holds
# no live task for the job (HA, a failed resume, an orphan row). The worker
# called it back to back until its deadline: 1.3M calls in 2 s with a fake.


class NoRunner(Reviews):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def wait(self, job_id: str, timeout: float) -> str:
        self.calls += 1
        return "running"


async def test_a_wait_that_returns_at_once_is_paced_not_spun():
    queue = FakeQueue([review_task("t1", 1)])
    reviews = NoRunner()
    result = await _work(queue, reviews, poll_s=0.02, deadline_s=0.3, stalled_s=10)
    assert reviews.calls <= 20  # one per poll interval, not millions
    assert result["outcomes"][0]["status"] == "unfinished"


async def test_a_job_with_no_runner_here_is_handed_back_early():
    queue = FakeQueue([review_task("t1", 1)])
    reviews = NoRunner()
    result = await _work(queue, reviews, poll_s=0.01, deadline_s=60, stalled_s=0.1)
    out = result["outcomes"][0]
    assert out["status"] == "unfinished" and "no runner" in out["resolution"]
    assert queue.resolved == {}  # left IN_PROGRESS for the orphan path
    assert result["elapsed_s"] < 5


# ── The CRM refusing a write ──────────────────────────────────────────


class RefusingQueue(FakeQueue):
    async def resolve(self, task_id: str, resolution: str) -> None:
        raise RuntimeError("Cannot resolve from TODO")


async def test_a_refused_resolve_is_surfaced_in_the_outcome():
    queue = RefusingQueue([review_task("t1", 1)])
    reviews = Reviews()
    result = await _work(queue, reviews)
    out = result["outcomes"][0]
    assert out["status"] == "posted"  # the review itself went out, once
    assert "task not resolved" in out["error"] and "Cannot resolve" in out["error"]
    assert reviews.finalized == [(REPO, 1, "job-1")]
    assert result["task_errors"] == [f"t1: {out['error']}"]


async def test_a_refused_start_reviews_nothing():
    class NoStart(FakeQueue):
        async def start(self, task_id: str) -> None:
            raise RuntimeError("start t1: bad transition")

    reviews = Reviews()
    result = await _work(NoStart([review_task("t1", 1)]), reviews)
    assert reviews.prepared == []
    assert result["outcomes"][0]["status"] == "not_started"
    assert "bad transition" in result["outcomes"][0]["error"]
