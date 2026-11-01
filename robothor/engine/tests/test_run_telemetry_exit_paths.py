"""Run telemetry is published on every run exit path, not just success.

``AgentRunner._finish_run`` is the one door every exit (completed, failed,
timed out, aborted, interrupted) goes through, so the run's trace is
published from there: once per trace, with the run's terminal status, and
with an error span status and the reason on anything but a clean finish.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.telemetry import TraceContext

EXIT_KINDS = [
    pytest.param(RunStatus.COMPLETED, None, "ok", id="completed"),
    pytest.param(RunStatus.FAILED, "provider returned 500", "error", id="failed"),
    pytest.param(RunStatus.TIMEOUT, "run exceeded its 300s deadline", "error", id="timed-out"),
    pytest.param(RunStatus.CANCELLED, "aborted: stalled for 600s", "error", id="aborted"),
    pytest.param(RunStatus.CANCELLED, "interrupted by the user", "error", id="interrupted"),
]


@pytest.fixture
def runner() -> AgentRunner:
    return AgentRunner(EngineConfig())


def _run(status: RunStatus, error: str | None) -> AgentRun:
    return AgentRun(
        id="00000000-0000-0000-0000-00000000tel1",
        tenant_id="default",
        agent_id="example-agent",
        status=status,
        error_message=error,
        model_used="example/model",
        input_tokens=100,
        output_tokens=20,
        duration_ms=4321,
    )


def _finish(runner: AgentRunner, run: AgentRun, trace: TraceContext) -> list[dict]:
    published: list[dict] = []
    with (
        patch.object(AgentRunner, "_persist_run_sync"),
        patch.object(AgentRunner, "_persist_run"),
        patch.object(trace, "publish_metrics", side_effect=published.append),
    ):
        runner._finish_run(run, trace=trace)
    return published


def _summary(trace: TraceContext):
    spans = [s for s in trace.spans if s.name == "run_summary"]
    assert len(spans) == 1, f"expected one run_summary span, got {len(spans)}"
    return spans[0]


@pytest.mark.parametrize(("status", "reason", "span_status"), EXIT_KINDS)
def test_every_exit_publishes_once_with_its_status(runner, status, reason, span_status):
    trace = TraceContext(run_id="r1", agent_id="example-agent")

    published = _finish(runner, _run(status, reason), trace)

    assert len(published) == 1
    assert published[0]["status"] == status.value
    assert published[0]["duration_ms"] == 4321
    span = _summary(trace)
    assert span.status == span_status
    assert span.attributes["run.status"] == status.value


@pytest.mark.parametrize(
    ("status", "reason", "span_status"), [p for p in EXIT_KINDS if p.id != "completed"]
)
def test_error_exits_carry_the_reason_in_the_otlp_span(runner, status, reason, span_status):
    trace = TraceContext(run_id="r1", agent_id="example-agent")

    _finish(runner, _run(status, reason), trace)

    otlp = trace.build_otlp_payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    summary = next(s for s in otlp if s["name"] == "run_summary")
    assert summary["status"] == {"code": 2, "message": reason}
    attrs = {
        a["key"]: a["value"]["stringValue"]
        for a in summary["attributes"]
        if "stringValue" in a["value"]
    }
    assert attrs["error.message"] == reason


def test_a_clean_finish_has_no_status_message(runner):
    trace = TraceContext(run_id="r1", agent_id="example-agent")

    _finish(runner, _run(RunStatus.COMPLETED, None), trace)

    otlp = trace.build_otlp_payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    summary = next(s for s in otlp if s["name"] == "run_summary")
    assert summary["status"] == {"code": 1}


def test_finishing_twice_publishes_once(runner):
    trace = TraceContext(run_id="r1", agent_id="example-agent")
    run = _run(RunStatus.FAILED, "boom")

    published = _finish(runner, run, trace)
    published += _finish(runner, run, trace)

    assert len(published) == 1
    _summary(trace)


def test_no_trace_is_a_noop(runner):
    with patch.object(AgentRunner, "_persist_run_sync"), patch.object(AgentRunner, "_persist_run"):
        runner._finish_run(_run(RunStatus.FAILED, "boom"), trace=None)


def test_a_run_publishes_once_even_from_two_traces(runner):
    """Another call site publishing the same run must not double-count it."""
    run = _run(RunStatus.COMPLETED, None)
    first = TraceContext(run_id=run.id, agent_id="example-agent")
    second = TraceContext(run_id=run.id, agent_id="example-agent")

    published = _finish(runner, run, first)
    with patch.object(second, "publish_metrics", side_effect=published.append):
        AgentRunner._publish_run_telemetry(second, run)

    assert len(published) == 1


def test_different_runs_each_publish(runner):
    a = _run(RunStatus.COMPLETED, None)
    b = _run(RunStatus.COMPLETED, None)
    b.id = "00000000-0000-0000-0000-00000000tel2"

    published = _finish(runner, a, TraceContext(run_id=a.id, agent_id="example-agent"))
    published += _finish(runner, b, TraceContext(run_id=b.id, agent_id="example-agent"))

    assert len(published) == 2
