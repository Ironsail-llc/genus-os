"""Deferrals and mode changes must be countable, not just loggable.

2026-08-27. The whole 29-hour local-tier outage was diagnosed by querying the
database after the fact, because nothing exported which mode the fleet was in
or how much work was being held back. A deferral you can only find by grepping
the journal is a deferral nobody notices until an operator asks why an agent
stopped running.

The gauge is deliberately a gauge (state, not an event) and the counter is
labelled by mode, so an operator can tell shadow deferrals in observe from real
ones in enforce -- which is precisely the evidence a promotion decision needs.
"""

from robothor.engine import metrics


class TestTheMetricsExist:
    def test_the_mode_is_exported_as_state(self):
        assert hasattr(metrics, "EXECUTION_MODE")

    def test_deferrals_are_counted_by_mode(self):
        assert hasattr(metrics, "ADMISSION_DEFERRALS_TOTAL")


class TestTheyDescribeTheFleetHonestly:
    def test_the_mode_gauge_distinguishes_cloud_from_local(self):
        metrics.set_execution_mode("local")
        assert metrics.EXECUTION_MODE.labels(mode="local")._value.get() == 1
        assert metrics.EXECUTION_MODE.labels(mode="cloud")._value.get() == 0

    def test_switching_back_clears_the_previous_mode(self):
        """Both modes reading 1 would make every dashboard lie."""
        metrics.set_execution_mode("local")
        metrics.set_execution_mode("cloud")
        assert metrics.EXECUTION_MODE.labels(mode="cloud")._value.get() == 1
        assert metrics.EXECUTION_MODE.labels(mode="local")._value.get() == 0

    def test_a_shadow_deferral_is_countable_separately_from_a_real_one(self):
        before = metrics.ADMISSION_DEFERRALS_TOTAL.labels(
            mode="observe", priority="background"
        )._value.get()
        metrics.record_admission_deferral("observe", "background")
        after = metrics.ADMISSION_DEFERRALS_TOTAL.labels(
            mode="observe", priority="background"
        )._value.get()
        assert after == before + 1

    def test_recording_never_raises_on_a_bad_label(self):
        """Telemetry must not be able to break the gate it observes."""
        metrics.record_admission_deferral(None, None)
        metrics.set_execution_mode("nonsense-mode")


class TestTheyAreActuallyWired:
    def test_the_deferral_counter_has_a_production_caller(self):
        import ast
        import pathlib

        import robothor.engine.admission_evidence as ev

        tree = ast.parse(pathlib.Path(ev.__file__).read_text())
        called = {
            getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert "record_admission_deferral" in called, (
            "the deferral counter has no production caller — the same state "
            "FleetPool itself shipped in"
        )


class TestTheGaugeFollowsTheTracker:
    def test_entering_local_moves_the_gauge(self, monkeypatch):
        """A gauge wired to nothing reports the boot mode forever."""
        monkeypatch.delenv("ROBOTHOR_EXECUTION_MODE", raising=False)
        from robothor.engine.execution_mode import LOCAL_STREAK_TO_ENTER, ExecutionModeTracker

        metrics.set_execution_mode("cloud")
        t = ExecutionModeTracker()
        for _ in range(LOCAL_STREAK_TO_ENTER):
            t.record_completion("ollama_chat/qwen3.8:27b")
        assert metrics.EXECUTION_MODE.labels(mode="local")._value.get() == 1
        assert metrics.EXECUTION_MODE.labels(mode="cloud")._value.get() == 0


class TestTheGaugeHasAValueBeforeAnythingHappens:
    def test_a_fresh_tracker_publishes_its_starting_mode(self, monkeypatch):
        """Found by scraping /metrics after a restart, not by a passing test.

        A labelled Prometheus series does not exist until a label combination is
        written. Exporting only on a TRANSITION meant the metric appeared with
        HELP and TYPE and no value at all, so 'which mode are we in' was
        unanswerable from metrics until the mode happened to change -- which,
        during a steady 29-hour outage, is never.
        """
        monkeypatch.delenv("ROBOTHOR_EXECUTION_MODE", raising=False)
        from prometheus_client import REGISTRY

        from robothor.engine.execution_mode import ExecutionModeTracker

        for mode in ("cloud", "local"):
            REGISTRY.get_sample_value("robothor_execution_mode", {"mode": mode})

        ExecutionModeTracker()
        cloud = REGISTRY.get_sample_value("robothor_execution_mode", {"mode": "cloud"})
        local = REGISTRY.get_sample_value("robothor_execution_mode", {"mode": "local"})
        assert cloud is not None and local is not None, (
            "the gauge publishes no series until a transition; a scrape after "
            "restart reports no mode at all"
        )
        assert cloud == 1 and local == 0
