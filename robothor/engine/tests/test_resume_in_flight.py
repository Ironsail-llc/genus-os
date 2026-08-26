"""A restart interrupts a run; it does not have to destroy it.

On restart the daemon REAPS every run still marked 'running' — the work is
lost even though `CheckpointManager` holds its messages and scratchpad and
`_resume_from_checkpoint` can restore them. A competitive audit found this
the one durability axis where a SQLite-backed harness beats this
Postgres-backed platform: OpenClaw resumes in-flight runs on a charged
attempt budget; we reaped.

The rules, each guarding a specific way this could go wrong:

* **Charge the attempt BEFORE resuming.** A run that dies during resume must
  still have paid, or a crash loop resumes forever. The counter is a durable
  column for the same reason: an in-memory count resets on exactly the event
  it exists to survive.
* **Only runs with a checkpoint.** Without one there is nothing to restore
  and "resume" means "run again from scratch", which is a re-execution the
  operator never asked for.
* **Bounded concurrency.** A crash with forty in-flight runs must not start
  forty agents at boot.
* **Off by default.** This changes what a restart does to live work; it
  promotes off -> observe -> enforce like every other control here.
"""

from __future__ import annotations

from robothor.engine.resume import (
    MAX_RESUME_ATTEMPTS,
    ResumeCandidate,
    resumable,
    resume_enabled,
)


def _c(run_id="r1", attempts=0, has_checkpoint=True, agent_id="a1"):
    return ResumeCandidate(
        run_id=run_id, agent_id=agent_id, resume_attempts=attempts, has_checkpoint=has_checkpoint
    )


class TestTheFlag:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_RESUME_IN_FLIGHT", raising=False)
        assert not resume_enabled()

    def test_on_when_set(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RESUME_IN_FLIGHT", "1")
        assert resume_enabled()

    def test_nonsense_is_off(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RESUME_IN_FLIGHT", "maybe")
        assert not resume_enabled()


class TestChoosingWhatToResume:
    def test_a_checkpointed_run_under_budget_is_resumable(self):
        assert resumable([_c()]) == [_c()]

    def test_a_run_without_a_checkpoint_is_not(self):
        """Nothing to restore means 'run again from scratch' — a
        re-execution nobody asked for."""
        assert resumable([_c(has_checkpoint=False)]) == []

    def test_a_run_at_its_attempt_budget_is_not(self):
        assert resumable([_c(attempts=MAX_RESUME_ATTEMPTS)]) == []

    def test_a_run_over_budget_is_not(self):
        assert resumable([_c(attempts=MAX_RESUME_ATTEMPTS + 5)]) == []

    def test_the_budget_allows_more_than_one_try(self):
        """A single retry would not survive a restart during a deploy."""
        assert MAX_RESUME_ATTEMPTS >= 2

    def test_the_budget_is_small_enough_to_terminate(self):
        assert MAX_RESUME_ATTEMPTS <= 5

    def test_mixed_candidates_are_filtered_not_rejected_wholesale(self):
        good = _c("keep")
        out = resumable([_c("no-cp", has_checkpoint=False), good, _c("spent", attempts=99)])
        assert [c.run_id for c in out] == ["keep"]

    def test_an_empty_list_is_fine(self):
        assert resumable([]) == []


class TestConcurrencyIsBounded:
    def test_a_large_backlog_is_capped(self):
        from robothor.engine.resume import MAX_RESUME_CONCURRENCY, resume_batch

        many = [_c(f"r{i}") for i in range(200)]
        assert len(resume_batch(many)) <= MAX_RESUME_CONCURRENCY

    def test_the_cap_is_more_than_one(self):
        from robothor.engine.resume import MAX_RESUME_CONCURRENCY

        assert MAX_RESUME_CONCURRENCY >= 2

    def test_the_oldest_runs_go_first(self):
        """Whatever is resumed, do it in a stable order so a crash loop
        cannot starve the same run forever."""
        from robothor.engine.resume import resume_batch

        batch = resume_batch([_c("c"), _c("a"), _c("b")])
        assert [x.run_id for x in batch] == ["a", "b", "c"]


class TestTheDaemonConsultsIt:
    @staticmethod
    def _source() -> str:
        from pathlib import Path

        import robothor.engine.daemon as m

        return Path(m.__file__).read_text(encoding="utf-8")

    def test_startup_tries_resume_before_reaping(self):
        body = self._source()
        assert "resume_interrupted_runs" in body, "nothing drives resume at startup"
        assert body.index("resume_interrupted_runs") < body.index(
            "cleaned = await asyncio.to_thread(_cleanup_stale_runs)"
        ), "reaping runs first would destroy the runs resume exists to save"
