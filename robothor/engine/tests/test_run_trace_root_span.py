"""A run's trace has one root span that covers the whole run.

``agent_run`` opens when the run's trace is created and closes at run exit,
carrying the run's status and trigger type; every other span of the run
(``llm_call``, ``tool_call``, ``run_summary``) is its child. Spans carry an
OTel span kind (root SERVER, llm_call CLIENT, everything else INTERNAL) so
trace metrics downstream are named by kind, not ``Unspecified``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.telemetry import TraceContext

OTLP_KIND_INTERNAL = 1
OTLP_KIND_SERVER = 2
OTLP_KIND_CLIENT = 3


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = []
        registry.get_tool_names.return_value = []
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def published():
    """Every (trace, run_data) the run publishes."""
    captured: list[tuple[TraceContext, dict[str, Any]]] = []

    def _capture(self: TraceContext, run_data: dict[str, Any]) -> None:
        captured.append((self, run_data))

    with patch.object(TraceContext, "publish_metrics", autospec=True, side_effect=_capture):
        yield captured


async def _execute(runner, agent_config, completion):
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        return await runner.execute("test-agent", "hello", agent_config=agent_config)


def _int_usage(response: Any) -> Any:
    """Plain-int cache usage, as a provider reports it (MagicMock attrs are truthy)."""
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0
    response.usage.prompt_tokens_details = None
    return response


def _by_name(trace: TraceContext) -> dict[str, list]:
    out: dict[str, list] = {}
    for s in trace.spans:
        out.setdefault(s.name, []).append(s)
    return out


def _otlp_spans(trace: TraceContext) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for s in trace.build_otlp_payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        out.setdefault(s["name"], []).append(s)
    return out


@pytest.mark.usefixtures("_mock_run_persistence")
class TestARunHasOneRootSpan:
    async def test_tool_run_root_covers_the_run_and_parents_every_span(
        self, runner, sample_agent_config, mock_litellm_response, published
    ):
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({"status": "TODO"})
        first = _int_usage(mock_litellm_response(content=None, tool_calls=[tc]))
        first.choices[0].message.content = None
        second = _int_usage(mock_litellm_response(content="Found 3 tasks."))
        calls = 0

        async def completion(**kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.15)
            return first if calls == 1 else second

        runner.registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        run = await _execute(runner, sample_agent_config, completion)

        assert run.status == RunStatus.COMPLETED
        assert len(published) == 1
        trace, _ = published[0]
        spans = _by_name(trace)
        assert len(spans["agent_run"]) == 1
        root = spans["agent_run"][0]
        assert root.duration_ms >= 300, "the root span must cover both LLM calls"
        assert root.status == "ok"
        assert root.attributes["run.status"] == "completed"
        assert root.attributes["run.trigger_type"] == str(run.trigger_type)
        children = spans["llm_call"] + spans["tool_call"] + spans["run_summary"]
        assert len(spans["llm_call"]) == 2
        assert len(spans["tool_call"]) == 1
        assert all(s.parent_span_id == root.span_id for s in children)
        assert root.start_time <= min(s.start_time for s in children)
        assert root.end_time >= max(s.end_time for s in children)

        otlp = _otlp_spans(trace)
        assert otlp["agent_run"][0]["kind"] == OTLP_KIND_SERVER
        assert {s["kind"] for s in otlp["llm_call"]} == {OTLP_KIND_CLIENT}
        assert otlp["tool_call"][0]["kind"] == OTLP_KIND_INTERNAL
        assert otlp["run_summary"][0]["kind"] == OTLP_KIND_INTERNAL

    async def test_failed_run_root_is_an_error_with_the_reason(
        self, runner, sample_agent_config, published
    ):
        async def completion(**kwargs):
            raise Exception("Model unavailable")

        run = await _execute(runner, sample_agent_config, completion)

        assert run.status == RunStatus.FAILED
        assert len(published) == 1
        trace, _ = published[0]
        root = _by_name(trace)["agent_run"][0]
        assert root.status == "error"
        assert root.attributes["run.status"] == "failed"
        otlp_root = _otlp_spans(trace)["agent_run"][0]
        assert otlp_root["status"]["code"] == 2
        assert otlp_root["status"]["message"] == (run.error_message or "")[:500]

    async def test_timed_out_run_still_closes_its_root(
        self, runner, sample_agent_config, mock_litellm_response, published
    ):
        sample_agent_config.timeout_seconds = 1
        sample_agent_config.stall_timeout_seconds = 0

        async def completion(**kwargs):
            await asyncio.sleep(5)
            return _int_usage(mock_litellm_response())

        run = await _execute(runner, sample_agent_config, completion)

        assert run.status == RunStatus.TIMEOUT
        assert len(published) == 1
        trace, _ = published[0]
        root = _by_name(trace)["agent_run"][0]
        assert root.status == "error"
        assert root.attributes["run.status"] == "timeout"
        assert root.duration_ms >= 900


class TestTraceContextRoot:
    def test_root_is_open_until_the_run_ends(self):
        trace = TraceContext(run_id="r1", agent_id="example-agent")
        trace.start_run()
        with trace.span("llm_call"):
            pass
        assert [s.name for s in trace.spans] == ["llm_call"]

        trace.end_run(status="completed", trigger_type="manual")
        trace.end_run(status="completed", trigger_type="manual")

        names = [s.name for s in trace.spans]
        assert names.count("agent_run") == 1
        root = trace.spans[-1]
        assert trace.spans[0].parent_span_id == root.span_id
        assert root.end_time >= trace.spans[0].end_time

    def test_sub_agent_root_hangs_off_the_parent_span(self):
        trace = TraceContext(
            run_id="r2",
            agent_id="example-agent",
            trace_id="a" * 32,
            parent_trace_id="a" * 32,
            parent_span_id="b" * 16,
        )
        trace.start_run()
        trace.end_run(status="completed", trigger_type="sub_agent")

        root = _otlp_spans(trace)["agent_run"][0]
        assert root["parentSpanId"] == "b" * 16

    def test_no_root_without_start_run(self):
        trace = TraceContext(run_id="r1", agent_id="example-agent")
        with trace.span("x"):
            pass
        trace.end_run(status="completed", trigger_type="manual")
        assert [s.name for s in trace.spans] == ["x"]
        assert trace.spans[0].parent_span_id is None
