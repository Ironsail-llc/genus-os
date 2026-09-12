"""A benchmark run may not write into a production tenant. Any of them.

Incident 2026-09-12. The benchmark sandbox was ``off``, which the runbook
described as "sub-runs stay read-only". It was not true. Over eleven days the
daily benchmark runner executed ~220 child runs under the instance's own
tenant. The CRM deny-set held — ``create_task`` refused — but a fixture person
from a suite's ``tasks.yaml`` became 25 production ``memory_facts`` rows, and a
real agent read those back as established fact and created a production person
and eight production tasks from them. The operator found fictional people in
his CRM.

Four things had to be true for that, and each one gets a test here:

1. the tool deny-set did not name every tool that writes memory (and was empty
   anyway for an agent whose manifest restricts nothing);
2. the write path below the tools trusted whatever tenant it was handed;
3. ``is_benchmark_run`` judged a run by its ``trigger_detail`` string, so a
   detached harness that invented its own label was counted as production;
4. alerts raised inside a benchmark child landed in the operator's inbox.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import robothor.engine.tools.handlers.benchmark as benchmark_handlers
from robothor.engine.benchmark_sandbox import (
    EXTERNAL_SIDE_EFFECT_TOOLS,
    MEMORY_WRITE_TOOLS,
    benchmark_allowed_tools,
    sandbox_tenant_id,
)
from robothor.engine.run_context import benchmark_run_scope
from robothor.engine.tools.handlers.memory import _MEMORY_MUTATING_TOOLS

_PRODUCTION = "an-instance-tenant"


# ─── 1. The deny-set, derived rather than hand-copied ────────────────────────

_HANDLER_DIR = Path(benchmark_handlers.__file__).resolve().parent

#: Symbols that perform a durable memory write. A handler that reaches one of
#: these, directly or through a helper in its own module, writes memory.
_MEMORY_WRITE_SYMBOLS = frozenset(
    {
        "enqueue_write",
        "store_fact",
        "store_facts_batch",
        "store_memory_content",
        "update_fact",
        "write_block",
        "append_to_block",
        "record_resolution",
    }
)

_MEMORY_SQL_WRITE = re.compile(
    r"(INSERT\s+INTO|UPDATE)\s+(memory_facts|agent_memory_blocks|memory_write_jobs)",
    re.IGNORECASE,
)


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                names.add(alias.asname or alias.name)
    return names


def _writes_memory_sql(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, str)
        and _MEMORY_SQL_WRITE.search(sub.value)
        for sub in ast.walk(node)
    )


def _tools_whose_handler_writes_memory() -> dict[str, str]:
    """Map tool name -> ``module:function``, derived from the handler sources.

    Static, deliberately. The alternative is a hand-written list beside the
    thing it describes, which is the drift that produced this incident and
    three others on this instance.
    """
    found: dict[str, str] = {}
    for path in sorted(_HANDLER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions: dict[str, ast.AST] = {}
        tools: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            functions[node.name] = node
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "_handler"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    tools[str(decorator.args[0].value)] = node.name
        calls = {name: _called_names(node) for name, node in functions.items()}
        writers = {
            name
            for name, node in functions.items()
            if _writes_memory_sql(node) or (calls[name] & _MEMORY_WRITE_SYMBOLS)
        }
        grew = True
        while grew:  # a handler that writes through a helper in the same module
            grew = False
            for name, called in calls.items():
                if name not in writers and (called & writers):
                    writers.add(name)
                    grew = True
        for tool, function in tools.items():
            if function in writers:
                found[tool] = f"{path.name}:{function}"
    return found


class TestMemoryWriteToolsAreDenied:
    def test_the_derivation_finds_something(self) -> None:
        """A scanner that silently matches nothing would pass every assertion
        below while proving none of them."""
        assert _tools_whose_handler_writes_memory()

    def test_no_tool_that_writes_memory_reaches_a_benchmark_child(self) -> None:
        derived = _tools_whose_handler_writes_memory()
        for sandbox in (False, True):
            allowed = benchmark_allowed_tools(sandbox=sandbox)
            leaked = sorted(set(derived) & allowed)
            assert not leaked, (
                f"with sandbox={sandbox} a benchmark sub-agent is handed tools whose "
                f"handler writes memory: "
                f"{[f'{name} ({derived[name]})' for name in leaked]}. "
                "Add them to EXTERNAL_SIDE_EFFECT_TOOLS / MEMORY_WRITE_TOOLS in "
                "robothor/engine/benchmark_sandbox.py."
            )

    def test_the_named_set_matches_what_the_memory_handlers_actually_do(self) -> None:
        """``MEMORY_WRITE_TOOLS`` is documentation, and documentation drifts.

        Scoped to ``handlers/memory.py``: the harness and experiment meta-tools
        reach a memory write several frames down and are denied by
        ``_BENCHMARK_EXCLUDED_TOOLS`` as meta-tools, not as memory writers.
        Every memory TOOL, though, has to be named here — otherwise a new one
        added tomorrow would be covered by nothing but the author's memory.
        """
        derived = _tools_whose_handler_writes_memory()
        from_memory_module = {
            tool for tool, where in derived.items() if where.startswith("memory.py:")
        }
        missing = sorted(from_memory_module - MEMORY_WRITE_TOOLS)
        assert not missing, f"memory-writing tools absent from MEMORY_WRITE_TOOLS: {missing}"

    def test_every_memory_write_tool_is_denied_in_every_mode(self) -> None:
        assert MEMORY_WRITE_TOOLS <= EXTERNAL_SIDE_EFFECT_TOOLS

    def test_every_memory_write_tool_also_refuses_at_its_handler(self) -> None:
        """The allow-list is computed once per suite; the handler guard runs on
        every call. Neither is sufficient alone, which is the whole lesson."""
        assert MEMORY_WRITE_TOOLS <= _MEMORY_MUTATING_TOOLS


class TestDenyListForAnUnrestrictedAgent:
    def test_an_agent_with_no_tools_allowed_still_gets_a_deny_list(self) -> None:
        """``tools_allowed: []`` means "everything", not "nothing".

        Subtracting the allow-list from an empty list produced an empty
        deny-list, so the least restricted agents were the least restricted
        benchmark children too.
        """
        denied = benchmark_handlers._benchmark_tools_denied(None, sandbox=False)
        assert "store_memory" in denied
        assert "memory_block_write" in denied
        assert "create_task" in denied

    def test_a_restricted_agent_is_still_only_denied_what_it_had(self) -> None:
        denied = set(benchmark_handlers._benchmark_tools_denied(["search_memory"], sandbox=False))
        assert "search_memory" not in denied  # a read it keeps
        assert "create_person" not in denied  # never had it


# ─── 2. The child run: every write refused, the run still completes ──────────

_WRITE_ATTEMPTS: list[tuple[str, dict[str, Any]]] = [
    ("store_memory", {"content": "Bob Quill prefers mornings"}),
    ("memory_block_write", {"block_name": "user_profile", "content": "Bob Quill"}),
    ("append_to_block", {"block_name": "user_profile", "entry": "Bob Quill"}),
    ("create_task", {"title": "call Bob Quill"}),
    ("update_task", {"task_id": "x", "status": "done"}),
    ("create_person", {"firstName": "Bob", "lastName": "Quill"}),
    ("log_interaction", {"contact_name": "Bob Quill", "channel": "email"}),
    ("record_resolution", {"open_item": "x", "outcome": "done"}),
    ("leave_breadcrumb", {"content": "Bob Quill"}),
]


class TestBenchmarkChildWithTheSandboxOff:
    """Requirement: with the sandbox off, a benchmark child's writes reach no
    tenant at all, each refusal is a tool RESULT rather than a crash, and the
    refusal keeps the shape every consumer already parses."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("tool", "args"), _WRITE_ATTEMPTS, ids=[t for t, _ in _WRITE_ATTEMPTS])
    async def test_every_write_is_refused_and_touches_no_database(
        self, tool: str, args: dict[str, Any], monkeypatch: Any
    ) -> None:
        from robothor.engine.tools import dispatch

        def _no_database(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError(f"{tool} reached the database from a benchmark run")

        monkeypatch.setattr(dispatch, "get_db", _no_database)
        monkeypatch.setattr("robothor.db.connection.get_connection", _no_database)

        with (
            patch(
                "robothor.engine.permissions.check_tool_permission",
                return_value=None,
            ),
            benchmark_run_scope(True, agent_id="agent-under-test", run_id="run-1"),
        ):
            result = await dispatch._execute_tool(
                tool,
                args,
                agent_id="agent-under-test",
                run_id="run-1",
                tenant_id=_PRODUCTION,
                is_benchmark=True,
            )

        assert isinstance(result, dict), "a refused tool must return a result, not raise"
        assert result.get("guard") == "is_benchmark"
        assert result.get("error", "").startswith("benchmark sandbox: ")
        assert tool in result["error"]

    @pytest.mark.asyncio
    async def test_the_same_tools_still_work_outside_a_benchmark_run(self) -> None:
        """The guard must be about benchmark runs, not about memory writes."""
        from robothor.engine.tools.handlers import memory as memory_handlers

        with patch(
            "robothor.engine.tools.handlers.memory.store_memory_content",
            new=AsyncMock(return_value={"facts_stored": 1}),
        ):
            ctx = _context(is_benchmark=False)
            result = await memory_handlers.HANDLERS["store_memory"]({"content": "a real fact"}, ctx)
        assert result == {"facts_stored": 1}


def _context(*, is_benchmark: bool) -> Any:
    from robothor.engine.tools.dispatch import ToolContext

    return ToolContext(
        agent_id="agent-under-test",
        run_id="run-1",
        tenant_id=_PRODUCTION,
        is_benchmark=is_benchmark,
    )


# ─── 3. Recording truth: what counts as benchmark traffic ────────────────────


class TestBenchmarkRunClassification:
    def test_the_harness_labels_every_child_with_the_benchmark_prefix(self) -> None:
        """Read off the harness source rather than asserted in prose: the
        ``trigger_detail`` every consumer filters on is built in one place."""
        from robothor.engine.analytics import BENCHMARK_TRIGGER_PREFIX

        source = Path(benchmark_handlers.__file__).read_text(encoding="utf-8")
        details = re.findall(r'trigger_detail=f?"([^"{]*)', source)
        assert details, "no trigger_detail literals found in the harness"
        assert all(d.startswith(BENCHMARK_TRIGGER_PREFIX) for d in details), details

    def test_the_trigger_detail_string_still_classifies(self) -> None:
        from robothor.engine.analytics import is_benchmark_run

        assert is_benchmark_run("benchmark:crm-hygiene:3") is True
        assert is_benchmark_run("telegram") is False
        assert is_benchmark_run(None) is False

    def test_a_flagged_run_classifies_whatever_it_called_itself(self) -> None:
        """A detached harness invented its own label (``p1m1:…``) and every
        consumer — dashboards, alerts, the runaway-token summary — counted its
        runs as production. The flag is the run's own claim about itself and is
        authoritative."""
        from robothor.engine.analytics import is_benchmark_run

        assert is_benchmark_run("p1m1:model-sweep", is_benchmark=True) is True
        assert is_benchmark_run(None, is_benchmark=True) is True
        assert is_benchmark_run("telegram", is_benchmark=False) is False


# ─── 4. Alerts raised inside a benchmark child ───────────────────────────────


class TestBenchmarkAlertsDoNotPageTheOperator:
    @pytest.mark.asyncio
    async def test_a_runaway_alert_in_a_benchmark_child_writes_a_benchmark_digest(
        self,
    ) -> None:
        from robothor.engine import alerts

        written: list[dict[str, Any]] = []

        def _send_notification(**kwargs: Any) -> str:
            written.append(kwargs)
            return "notif-1"

        paged = AsyncMock(return_value=True)
        with (
            patch("robothor.crm.dal.send_notification", side_effect=_send_notification),
            patch.object(alerts, "_send_telegram", paged),
            benchmark_run_scope(True, agent_id="agent-under-test", run_id="run-1"),
        ):
            delivered = await alerts.alert(
                "critical",
                "Runaway-token hard cap: agent-under-test",
                "run_id=run-1 tokens=5,000,000",
            )

        assert delivered is True
        assert paged.await_count == 0, "a benchmark child paged the operator"
        assert len(written) == 1
        assert written[0]["notification_type"] == alerts.BENCHMARK_DIGEST_TYPE
        assert "[benchmark]" in written[0]["subject"]

    @pytest.mark.asyncio
    async def test_the_same_alert_outside_a_benchmark_run_still_pages(self) -> None:
        from robothor.engine import alerts

        paged = AsyncMock(return_value=True)
        with patch.object(alerts, "_send_telegram", paged):
            delivered = await alerts.alert("critical", "PostgreSQL down", "3 ping failures")
        assert delivered is True
        assert paged.await_count == 1

    def test_the_heartbeat_alert_reader_does_not_read_the_benchmark_digest(self) -> None:
        from robothor.engine.alerts import BENCHMARK_DIGEST_TYPE
        from robothor.engine.warmup import ALERT_DIGEST_TYPES

        assert BENCHMARK_DIGEST_TYPE not in ALERT_DIGEST_TYPES

    def test_the_benchmark_digest_type_is_allowed_by_the_database(self) -> None:
        """135 of these rows in 14 hours is what turned a "Hello" into a
        six-minute triage — and a type the CHECK constraint rejects is silently
        dropped, which is how ``alert_digest`` was lost before migration 099."""
        from robothor.engine.alerts import BENCHMARK_DIGEST_TYPE

        migrations = sorted(Path("crm/migrations").glob("*.sql"))
        allowing = [
            path
            for path in migrations
            if "crm_agent_notifications_notification_type_check" in path.read_text(encoding="utf-8")
        ]
        assert allowing, "no migration defines the notification_type CHECK"
        assert BENCHMARK_DIGEST_TYPE in allowing[-1].read_text(encoding="utf-8")


# ─── The refusal never carries the fixture's content ─────────────────────────


class TestRefusalsAreContentFree:
    @pytest.mark.asyncio
    async def test_the_warning_names_identifiers_and_nothing_else(self, caplog: Any) -> None:
        from robothor.memory import facts as facts_mod

        secret = "Bob Quill (bob.quill@example.com) prefers mornings"
        with (
            caplog.at_level(logging.WARNING, logger="robothor.engine.run_context"),
            benchmark_run_scope(True, agent_id="agent-under-test", run_id="run-1"),
        ):
            stored = await facts_mod.store_facts_batch(
                [{"fact_text": secret, "category": "personal"}],
                secret,
                "conversation",
                tenant_id=_PRODUCTION,
            )
        assert stored == []
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert "agent-under-test" in logged
        assert "Bob Quill" not in logged
        assert "bob.quill@example.com" not in logged


def test_the_sandbox_tenant_is_the_only_tenant_a_benchmark_run_may_write() -> None:
    from robothor.engine.run_context import benchmark_write_refused

    with benchmark_run_scope(True, agent_id="a", run_id="r"):
        assert benchmark_write_refused(sandbox_tenant_id(), what="probe") is False
        assert benchmark_write_refused(_PRODUCTION, what="probe") is True
        assert benchmark_write_refused("", what="probe") is True
    assert benchmark_write_refused(_PRODUCTION, what="probe") is False
