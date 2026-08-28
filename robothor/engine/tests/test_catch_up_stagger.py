"""Catching up after downtime must not become the next outage.

2026-08-27. ``_catch_up_missed_runs`` spawned every missed agent at once. That
is a real bug independent of execution mode: it fires after ANY downtime, and
the machine it fires on serves two inference slots. A restart that missed six
crons launched six simultaneous runs at a device that can serve two, which is
how the 21:00 restart turned into a queue.

The fix is a stagger, not a queue -- the existing catch_up / stale_after_minutes
semantics already decide *whether* to run; this only decides *when*.

Also pinned here: a deferred tick must not advance ``last_run_at``. That single
omission is what makes deferral safe, because the existing coalescing machinery
then treats the tick as a missed fire and retries it. If the skip path ever
advances the schedule, a deferred agent is silently DROPPED rather than delayed,
and nothing in the system would report it.
"""

import ast
import inspect
import pathlib

from robothor.engine import scheduler as sched


class TestCatchUpIsStaggered:
    def test_a_stagger_interval_exists_and_is_positive(self):
        assert sched.CATCH_UP_STAGGER_SECONDS > 0

    def test_spawns_are_staggered_not_simultaneous(self, monkeypatch):
        """Six missed agents must not all launch at t=0."""
        delays = []

        def fake_one(self, agent_config, delay_seconds=0.0):
            delays.append(delay_seconds)
            return True

        monkeypatch.setattr(sched.CronScheduler, "_catch_up_one", fake_one)
        configs = [type("C", (), {"id": f"agent-{i}"})() for i in range(6)]
        sched.CronScheduler._catch_up_missed_runs(object.__new__(sched.CronScheduler), configs)
        assert delays == sorted(delays), "delays must be monotonically increasing"
        assert delays[0] == 0, "the first catch-up pays no latency tax"
        assert delays[-1] > 0, "later catch-ups must actually be delayed"
        assert len(set(delays)) == len(delays), "every agent needs its own slot"

    def test_a_single_missed_agent_pays_no_latency_tax(self, monkeypatch):
        delays = []
        monkeypatch.setattr(
            sched.CronScheduler,
            "_catch_up_one",
            lambda self, cfg, delay_seconds=0.0: delays.append(delay_seconds) or True,
        )
        sched.CronScheduler._catch_up_missed_runs(
            object.__new__(sched.CronScheduler), [type("C", (), {"id": "solo"})()]
        )
        assert delays == [0]

    def test_an_agent_that_does_not_need_catching_up_consumes_no_slot(self, monkeypatch):
        """Only actual spawns advance the stagger, or one skipped agent would
        push every later one further out for no reason."""
        delays = []

        def fake_one(self, agent_config, delay_seconds=0.0):
            delays.append(delay_seconds)
            return agent_config.id != "skipped"

        monkeypatch.setattr(sched.CronScheduler, "_catch_up_one", fake_one)
        configs = [
            type("C", (), {"id": "first"})(),
            type("C", (), {"id": "skipped"})(),
            type("C", (), {"id": "third"})(),
        ]
        sched.CronScheduler._catch_up_missed_runs(object.__new__(sched.CronScheduler), configs)
        assert delays[2] == sched.CATCH_UP_STAGGER_SECONDS

    def test_one_bad_schedule_still_does_not_stop_the_others(self, monkeypatch):
        def fake_one(self, agent_config, delay_seconds=0.0):
            if agent_config.id == "bad":
                raise ValueError("invalid cron")
            return True

        monkeypatch.setattr(sched.CronScheduler, "_catch_up_one", fake_one)
        configs = [type("C", (), {"id": n})() for n in ("bad", "good")]
        sched.CronScheduler._catch_up_missed_runs(
            object.__new__(sched.CronScheduler), configs
        )  # must not raise


class TestDeferralDoesNotDropTheTick:
    def test_nothing_that_advances_the_schedule_runs_before_the_gate(self):
        """A deferred tick must be retried, not lost.

        ``update_schedule_state`` is reached only through ``_record_timeout``
        and ``_execute_and_deliver``; the invariant is that neither is called
        before admission has said yes, and that ``_run_scheduled`` never
        advances the schedule itself. Asserted on the AST rather than on text,
        because a grep here would match the import.
        """
        source = pathlib.Path(inspect.getfile(sched)).read_text()
        tree = ast.parse(source)

        advancers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "update_schedule_state"
                for sub in ast.walk(node)
            )
        }
        assert advancers, "expected some function to advance the schedule"

        for node in ast.walk(tree):
            if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_scheduled"):
                continue
            admit_line = None
            advancing_calls = []
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if name == "admit":
                    admit_line = sub.lineno
                elif name in advancers:
                    advancing_calls.append((name, sub.lineno))
                elif name == "update_schedule_state":
                    raise AssertionError(
                        "_run_scheduled advances the schedule directly; a deferred "
                        "tick would be dropped instead of retried"
                    )
            assert admit_line, "_run_scheduled does not consult admission"
            assert advancing_calls, "expected _run_scheduled to reach an advancing call"
            for name, line in advancing_calls:
                assert line > admit_line, (
                    f"{name} runs before admission — a deferred tick would advance "
                    "last_run_at and be silently dropped rather than retried"
                )
            return
        raise AssertionError("_run_scheduled not found")
