"""n8n_list_workflows / n8n_recent_failures: read the snapshot, never n8n itself."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.integrations.n8n.store import MemoryN8nStore

BASE = "https://n8n.example.com"
T = "test-tenant"
CTX = ToolContext(agent_id="main", tenant_id=T)
NOW = datetime.now(UTC)


@pytest.fixture
def configure(monkeypatch):
    from robothor.settings import reset_settings

    def _set(**pairs: str) -> None:
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    for name in ("ROBOTHOR_N8N_ENABLED", "ROBOTHOR_N8N_BASE_URL", "ROBOTHOR_N8N_POLL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield _set
    reset_settings()


@pytest.fixture
def store(monkeypatch, configure):
    import httpx

    from robothor.engine.tools.handlers import n8n as handler

    configure(ROBOTHOR_N8N_ENABLED="true", ROBOTHOR_N8N_BASE_URL=BASE)
    memory = MemoryN8nStore()
    monkeypatch.setattr(handler, "_store", lambda: memory)

    def no_network(*_a, **_k):
        raise AssertionError("the n8n tools read the snapshot; they never call n8n")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    return memory


async def _seed(store: MemoryN8nStore) -> None:
    def wf(wid, name, active, tags):
        return {
            "workflow_id": wid,
            "name": name,
            "active": active,
            "tags": tags,
            "remote_updated_at": None,
        }

    def ex(eid, wid, hours_ago):
        at = NOW - timedelta(hours=hours_ago)
        return {
            "execution_id": eid,
            "workflow_id": wid,
            "status": "error",
            "mode": "trigger",
            "started_at": at,
            "stopped_at": at,
        }

    await store.upsert_workflows(
        T,
        [
            wf("wf1", "Invoice sync", True, ["billing"]),
            wf("wf2", "Lead router", True, ["sales"]),
            wf("wf3", "Old export", False, []),
        ],
    )
    await store.record_executions(T, [ex("1", "wf1", 1), ex("2", "wf2", 30), ex("3", "wf1", 3)])
    await store.set_error_messages(T, {"1": "HTTP 500 from billing API"})
    await store.set_sync_state(T, polled_at=NOW, ok=True, workflows_total=3, workflows_active=2)


def test_the_tools_are_registered_read_only_and_schematised() -> None:
    from robothor.engine.tools import READONLY_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    handlers = builtin_handlers()
    with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
        schemas = get_engine_schemas()
    for name in ("n8n_list_workflows", "n8n_recent_failures"):
        assert name in handlers
        assert name in READONLY_TOOLS
        assert schemas[name]["function"]["name"] == name
    props = schemas["n8n_list_workflows"]["function"]["parameters"]["properties"]
    assert props["active_only"]["type"] == "boolean" and props["query"]["type"] == "string"
    props = schemas["n8n_recent_failures"]["function"]["parameters"]["properties"]
    assert props["hours"]["type"] == "integer" and props["workflow_id"]["type"] == "string"


async def test_list_workflows_defaults_to_active_ones_with_counts(store) -> None:
    from robothor.engine.tools.handlers.n8n import HANDLERS

    await _seed(store)
    result = await HANDLERS["n8n_list_workflows"]({}, CTX)

    assert result["status"] == "warn"
    assert [w["name"] for w in result["workflows"]] == ["Invoice sync", "Lead router"]
    invoice = result["workflows"][0]
    assert invoice["failures_24h"] == 2
    assert invoice["last_error"] == "HTTP 500 from billing API"
    assert invoice["url"] == f"{BASE}/workflow/wf1"
    assert result["count"] == 2


async def test_list_workflows_filters_by_name_or_tag_and_can_include_inactive(store) -> None:
    from robothor.engine.tools.handlers.n8n import HANDLERS

    await _seed(store)
    by_tag = await HANDLERS["n8n_list_workflows"]({"query": "SALES"}, CTX)
    assert [w["workflow_id"] for w in by_tag["workflows"]] == ["wf2"]
    every = await HANDLERS["n8n_list_workflows"]({"active_only": False, "query": "export"}, CTX)
    assert [w["workflow_id"] for w in every["workflows"]] == ["wf3"]


async def test_recent_failures_window_and_workflow_filter(store) -> None:
    from robothor.engine.tools.handlers.n8n import HANDLERS

    await _seed(store)
    day = await HANDLERS["n8n_recent_failures"]({}, CTX)
    assert [f["execution_id"] for f in day["failures"]] == ["1", "3"]
    first = day["failures"][0]
    assert first["workflow_id"] == "wf1"
    assert first["workflow_name"] == "Invoice sync"
    assert first["url"] == f"{BASE}/workflow/wf1/executions/1"
    assert first["error_message"] == "HTTP 500 from billing API"

    two_days = await HANDLERS["n8n_recent_failures"]({"hours": 48}, CTX)
    assert [f["execution_id"] for f in two_days["failures"]] == ["1", "3", "2"]

    only = await HANDLERS["n8n_recent_failures"]({"hours": 48, "workflow_id": "wf2"}, CTX)
    assert [f["execution_id"] for f in only["failures"]] == ["2"]


async def test_the_tools_are_tenant_scoped(store) -> None:
    from robothor.engine.tools.handlers.n8n import HANDLERS

    await _seed(store)
    other = ToolContext(agent_id="main", tenant_id="other-tenant")
    result = await HANDLERS["n8n_recent_failures"]({"hours": 48}, other)
    assert result["failures"] == []


async def test_when_the_monitor_is_off_the_tools_say_so(configure, monkeypatch) -> None:
    from robothor.engine.tools.handlers import n8n as handler

    configure()

    def no_store():
        raise AssertionError("an unconfigured monitor has nothing to read")

    monkeypatch.setattr(handler, "_store", no_store)
    for name in ("n8n_list_workflows", "n8n_recent_failures"):
        result = await handler.HANDLERS[name]({}, CTX)
        assert result["status"] == "not_configured"
        assert "ROBOTHOR_N8N_ENABLED" in result["message"]


async def test_bad_arguments_are_coerced_not_crashed(store) -> None:
    from robothor.engine.tools.handlers.n8n import HANDLERS

    await _seed(store)
    result = await HANDLERS["n8n_recent_failures"]({"hours": "nonsense"}, CTX)
    assert result["hours"] == 24
    huge = await HANDLERS["n8n_recent_failures"]({"hours": 10_000}, CTX)
    assert huge["hours"] == 720


def test_a_benchmark_run_never_gets_the_n8n_reads() -> None:
    """They read the operator's REAL automations' snapshot and settings, so a
    graded run is withheld them like config_get and the Claude Code reads."""
    from robothor.engine.tools.handlers.benchmark import (
        _BENCHMARK_WITHHELD_READS,
        benchmark_readonly_tools,
    )

    for name in ("n8n_list_workflows", "n8n_recent_failures"):
        assert name in _BENCHMARK_WITHHELD_READS
        assert name not in benchmark_readonly_tools()
