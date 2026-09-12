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
import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import robothor.engine.tools.handlers.benchmark as benchmark_handlers
from robothor.engine.benchmark_sandbox import (
    BOUNDARY_GUARDED_TOOLS,
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
        # A READ tool that writes is still a write. search_memory lands in
        # log_fact_access once per consulted fact, and those rows are the decay
        # scorer's only input — the gap that survived the first fix.
        "log_fact_access",
        "bump_failure_for_run",
    }
)

_MEMORY_SQL_WRITE = re.compile(
    r"(INSERT\s+INTO|UPDATE)\s+"
    r"(memory_facts|agent_memory_blocks|memory_write_jobs|fact_access_log)",
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
            leaked = sorted((set(derived) & allowed) - BOUNDARY_GUARDED_TOOLS)
            assert not leaked, (
                f"with sandbox={sandbox} a benchmark sub-agent is handed tools whose "
                f"handler writes memory: "
                f"{[f'{name} ({derived[name]})' for name in leaked]}. "
                "Add them to EXTERNAL_SIDE_EFFECT_TOOLS / MEMORY_WRITE_TOOLS in "
                "robothor/engine/benchmark_sandbox.py — or, if the tool must stay "
                "and its write is guarded at the boundary, to BOUNDARY_GUARDED_TOOLS "
                "with a test proving the guard."
            )

    def test_every_boundary_guarded_exception_really_does_write(self) -> None:
        """The exception list may only excuse a tool the scanner FOUND.

        A name added here that writes nothing is a name someone will later read
        as "this was considered and is fine" — the exception list has to stay
        an admission of a real write, not a mute button.
        """
        derived = _tools_whose_handler_writes_memory()
        bogus = sorted(BOUNDARY_GUARDED_TOOLS - set(derived))
        assert not bogus, f"BOUNDARY_GUARDED_TOOLS names tools that write nothing: {bogus}"

    def test_every_boundary_guarded_exception_is_actually_reachable(self) -> None:
        """...and only excuses a tool a benchmark child is really handed."""
        allowed = benchmark_allowed_tools(sandbox=False) | benchmark_allowed_tools(sandbox=True)
        stale = sorted(BOUNDARY_GUARDED_TOOLS - allowed)
        assert not stale, f"BOUNDARY_GUARDED_TOOLS excuses tools already denied: {stale}"

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
        missing = sorted(from_memory_module - MEMORY_WRITE_TOOLS - BOUNDARY_GUARDED_TOOLS)
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

    def test_the_enumeration_failing_does_not_empty_the_deny_list(self, monkeypatch: Any) -> None:
        """Fail CLOSED.

        The first version of this fallback swallowed every exception and
        returned an empty set, which put the deny-list back at 2 entries — with
        `exec` and `invoke_skill` NOT denied. That is the documented 2026-05-28
        escape (invoke_skill -> exec -> a real email to a real contact),
        reachable again through the remedy's own error path behind one WARNING
        nothing gates on.
        """

        def _explode(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(
            "robothor.engine.tools.registry.ToolRegistry.registered_tool_names", _explode
        )
        denied = set(benchmark_handlers._benchmark_tools_denied(None, sandbox=False))

        assert len(denied) > 2
        for tool in ("exec", "invoke_skill", "write_file", "store_memory", "spawn_agent"):
            assert tool in denied, f"{tool} is not denied when enumeration fails"

    def test_the_static_fallback_covers_every_statically_known_deny_set(
        self, monkeypatch: Any
    ) -> None:
        from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS

        def _explode(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(
            "robothor.engine.tools.registry.ToolRegistry.registered_tool_names", _explode
        )
        denied = set(benchmark_handlers._benchmark_tools_denied(None, sandbox=False))
        expected = EXTERNAL_SIDE_EFFECT_TOOLS | MEMORY_WRITE_TOOLS | _BENCHMARK_EXCLUDED_TOOLS
        assert expected - benchmark_allowed_tools(sandbox=False) <= denied


class TestAdapterToolsAreDeniedToBenchmarkChildren:
    """Adapter / MCP tools are external side effects by nature.

    They are registered at runtime from a server's ``tools/list``, so no static
    set can name them; and ``dispatch._execute_tool`` routes them to the MCP
    session BEFORE ``ToolContext`` exists, so no ``ctx.is_benchmark`` gate ever
    runs and the memory boundary is in another process entirely. The only place
    they can be stopped is where the child's tool list is built.
    """

    @staticmethod
    def _register_fake_adapter(monkeypatch: Any, name: str = "acme_delete_patient") -> None:
        from robothor.engine.tools import get_registry

        registry = get_registry()
        monkeypatch.setitem(
            registry._schemas,
            name,
            {"type": "function", "function": {"name": name, "parameters": {}}},
        )
        monkeypatch.setitem(registry._adapter_routes, name, "acme")

    def test_an_adapter_tool_is_denied_to_an_unrestricted_agent(self, monkeypatch: Any) -> None:
        self._register_fake_adapter(monkeypatch)
        denied = set(benchmark_handlers._benchmark_tools_denied(None, sandbox=False))
        assert "acme_delete_patient" in denied

    def test_an_adapter_tool_is_denied_even_when_the_manifest_grants_it(
        self, monkeypatch: Any
    ) -> None:
        self._register_fake_adapter(monkeypatch)
        denied = set(
            benchmark_handlers._benchmark_tools_denied(
                ["acme_delete_patient", "search_memory"], sandbox=True
            )
        )
        assert "acme_delete_patient" in denied
        assert "search_memory" not in denied

    def test_an_adapter_tool_is_never_in_the_allow_list(self, monkeypatch: Any) -> None:
        self._register_fake_adapter(monkeypatch)
        for sandbox in (False, True):
            assert "acme_delete_patient" not in benchmark_allowed_tools(sandbox=sandbox)

    def test_the_enumeration_sees_adapter_tools(self, monkeypatch: Any) -> None:
        """The previous version read `robothor.api.mcp.get_tool_definitions()`
        and `get_engine_schemas()`, neither of which contains a runtime-
        registered adapter tool — so its docstring's claim to name "every tool
        this instance registers" was false on an instance with adapters."""
        self._register_fake_adapter(monkeypatch)
        assert "acme_delete_patient" in benchmark_handlers._every_registered_tool()


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

    @pytest.mark.asyncio
    async def test_the_helper_routes_on_the_run_row(self) -> None:
        """The out-of-band detectors run on the daemon's loop, not inside the
        graded child, so ``in_benchmark_run()`` is False there. They have to
        route on the run they are alerting ABOUT."""
        from robothor.engine import alerts

        written: list[dict[str, Any]] = []
        paged = AsyncMock(return_value=True)
        with (
            patch(
                "robothor.crm.dal.send_notification",
                side_effect=lambda **kw: written.append(kw) or "n",
            ),
            patch.object(alerts, "_send_telegram", paged),
        ):
            await alerts.alert_about_run(
                "warning", "Runaway-burn (out-of-band)", "b", trigger_detail="benchmark:s:1"
            )
            await alerts.alert_about_run("warning", "Runaway-burn (out-of-band)", "b")

        assert [w["notification_type"] for w in written] == [
            alerts.BENCHMARK_DIGEST_TYPE,
            "alert_digest",
        ]
        assert "[benchmark]" in written[0]["subject"]
        assert "[benchmark]" not in written[1]["subject"]

    @pytest.mark.asyncio
    async def test_the_runaway_burn_detector_does_not_page_about_a_graded_child(self) -> None:
        from robothor.engine import detectors

        written: list[dict[str, Any]] = []
        rows = [
            {
                "id": "r1",
                "agent_id": "a",
                "model_used": "m",
                "input_tokens": 600_000,
                "output_tokens": 0,
                "elapsed_s": 10,
                "trigger_detail": "benchmark:suite:1",
            }
        ]
        with (
            patch.object(detectors, "check_runaway_burn", return_value=rows),
            patch.object(detectors, "_should_fire", return_value=True),
            patch(
                "robothor.crm.dal.send_notification",
                side_effect=lambda **kw: written.append(kw) or "n",
            ),
        ):
            assert await detectors.runaway_burn_detector() == 1

        assert [w["notification_type"] for w in written] == ["benchmark_digest"]

    @pytest.mark.asyncio
    async def test_the_zombie_detector_still_pages_about_a_production_run(self) -> None:
        from robothor.engine import detectors

        written: list[dict[str, Any]] = []
        rows = [
            {
                "id": "r1",
                "agent_id": "a",
                "age_s": 900,
                "last_step_at": None,
                "trigger_detail": "telegram",
            }
        ]
        with (
            patch.object(detectors, "check_zombie_runners", return_value=rows),
            patch.object(detectors, "_should_fire", return_value=True),
            patch(
                "robothor.crm.dal.send_notification",
                side_effect=lambda **kw: written.append(kw) or "n",
            ),
        ):
            assert await detectors.zombie_runner_detector() == 1

        assert [w["notification_type"] for w in written] == ["alert_digest"]

    def test_every_detector_alert_routes_through_the_helper(self) -> None:
        """All eight sites, not the two with a test.

        A detector added tomorrow that calls ``alert()`` directly would page
        the operator about a graded child again, and nothing here would notice.
        """
        source = Path(__import__("robothor.engine.detectors", fromlist=["x"]).__file__).read_text(
            encoding="utf-8"
        )
        bare = re.findall(r"(?<!_about_run)\bawait alert\(", source)
        assert not bare, (
            f"{len(bare)} detector alert site(s) bypass alerts.alert_about_run — "
            "an alert about a benchmark run would reach the operator's inbox"
        )

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


# ─── 5. The shared soft-runaway batch ────────────────────────────────────────


class TestSoftRunawayBatchIsNotMisrouted:
    """``_soft_runaway_pending`` is a MODULE-GLOBAL list shared by every
    concurrent run, and it is flushed by whichever run happens to cross the
    threshold next — in that run's context.

    So routing the flush on the CURRENT context can send a summary about real
    production runs to ``benchmark_digest``, which the heartbeat does not read
    and nothing acks: a genuine runaway-token page, silently lost. That is a
    worse outcome than the noise this change set out to remove.
    """

    @pytest.fixture(autouse=True)
    def _clean_batch(self) -> Any:
        from robothor.engine import runner as runner_mod

        runner_mod._soft_runaway_pending = []
        runner_mod._soft_runaway_window_started_at = None
        yield
        runner_mod._soft_runaway_pending = []
        runner_mod._soft_runaway_window_started_at = None

    def test_a_benchmark_crossing_never_enters_the_shared_batch(self) -> None:
        from robothor.engine import runner as runner_mod

        spawned: list[Any] = []
        with (
            patch("robothor.engine.task_registry.get_task_registry") as registry,
            benchmark_run_scope(True, agent_id="graded", run_id="r1"),
        ):
            registry.return_value.spawn.side_effect = lambda coro, **kw: (
                spawned.append(kw.get("name", "")),
                coro.close(),
            )
            runner_mod._send_soft_runaway_alert("graded", "r1", 600_000, "m", 0.1)

        assert runner_mod._soft_runaway_pending == [], (
            "a benchmark crossing joined the batch that production runs flush"
        )
        assert runner_mod._soft_runaway_window_started_at is None, (
            "a benchmark crossing opened the window that gates production pages — "
            "the next real crossing would be silently batched instead of paging"
        )
        assert spawned, "the benchmark crossing was dropped entirely"

    def test_a_production_batch_flushed_inside_a_benchmark_run_still_pages(self) -> None:
        """The interleaving that loses a real page.

        Production runs accrue a batch; a benchmark child crosses next. Before
        this fix the child's context decided the routing for the whole summary.
        """
        from robothor.engine import runner as runner_mod

        # A settable clock rather than an iterator: the benchmark crossing
        # returns before reading it, and a test whose timeline depends on how
        # many times the code under test happens to call the clock is a test
        # that breaks for the wrong reason.
        clock = {"t": 0.0}
        with patch.object(runner_mod, "_runaway_alert_clock", lambda: clock["t"]):
            with patch("robothor.engine.task_registry.get_task_registry") as registry:
                registry.return_value.spawn.side_effect = lambda coro, **kw: coro.close()
                # 1. a production run opens the window and pages.
                runner_mod._send_soft_runaway_alert("prod-a", "r1", 600_000, "m", 0.1)
                # 2. a second production run accrues silently inside it.
                clock["t"] = 1.0
                runner_mod._send_soft_runaway_alert("prod-b", "r2", 700_000, "m", 0.1)
                assert len(runner_mod._soft_runaway_pending) == 1

                # 3. a benchmark child crosses after the window expired. It must
                #    not flush — and must not be able to relabel prod-b's page.
                clock["t"] = 10_000.0
                with benchmark_run_scope(True, agent_id="graded", run_id="r3"):
                    runner_mod._send_soft_runaway_alert("graded", "r3", 800_000, "m", 0.1)
                assert [e["agent"] for e in runner_mod._soft_runaway_pending] == ["prod-b"], (
                    "the benchmark child took over the production batch"
                )

                # 4. the next PRODUCTION crossing flushes it — as an
                #    alert_digest the operator's heartbeat actually reads.
                written: list[dict[str, Any]] = []
                registry.return_value.spawn.side_effect = lambda coro, **kw: asyncio.run(coro)
                with patch(
                    "robothor.crm.dal.send_notification",
                    side_effect=lambda **kw: written.append(kw) or "n",
                ):
                    runner_mod._send_soft_runaway_alert("prod-c", "r4", 900_000, "m", 0.1)

                assert written, "the production batch was never flushed"
                assert [w["notification_type"] for w in written] == ["alert_digest"]
                assert "prod-b" in written[0]["body"], "prod-b's crossing was lost"


# ─── 6. The benchmark digest must not silt up the operator's inbox ───────────


class TestBenchmarkDigestStaysOutOfTheInbox:
    def test_get_agent_inbox_excludes_benchmark_digest_by_default(self) -> None:
        """These rows are addressed to the operator agent and nothing ever acks
        them, so an unfiltered ``get_inbox`` returns a monotonically growing
        pile — ~230/day at the incident's rate."""
        import inspect

        from robothor.crm import dal

        source = inspect.getsource(dal.get_agent_inbox)
        assert "benchmark_digest" in source, (
            "get_agent_inbox does not mention benchmark_digest, so it returns them"
        )

    def test_the_default_inbox_query_filters_the_type_out(self) -> None:
        from robothor.crm import dal
        from robothor.engine.alerts import BENCHMARK_DIGEST_TYPE

        captured: list[tuple[str, Any]] = []

        class _Cur:
            def execute(self, sql: str, params: Any = None) -> None:
                captured.append((sql, params))

            def fetchall(self) -> list[Any]:
                return []

        class _Conn:
            def cursor(self, *_a: Any, **_kw: Any) -> _Cur:
                return _Cur()

        class _Ctx:
            def __enter__(self) -> _Conn:
                return _Conn()

            def __exit__(self, *_a: Any) -> bool:
                return False

        with patch.object(dal, "get_connection", lambda: _Ctx()):
            dal.get_agent_inbox(agent_id="main", unread_only=True, limit=10)
        sql, params = captured[0]
        assert BENCHMARK_DIGEST_TYPE in sql or BENCHMARK_DIGEST_TYPE in str(params)

    def test_an_explicit_type_filter_still_returns_them(self) -> None:
        """Excluded by DEFAULT, not hidden: the rows are a queryable record."""
        from robothor.crm import dal
        from robothor.engine.alerts import BENCHMARK_DIGEST_TYPE

        captured: list[tuple[str, Any]] = []

        class _Cur:
            def execute(self, sql: str, params: Any = None) -> None:
                captured.append((sql, params))

            def fetchall(self) -> list[Any]:
                return []

        class _Conn:
            def cursor(self, *_a: Any, **_kw: Any) -> _Cur:
                return _Cur()

        class _Ctx:
            def __enter__(self) -> _Conn:
                return _Conn()

            def __exit__(self, *_a: Any) -> bool:
                return False

        with patch.object(dal, "get_connection", lambda: _Ctx()):
            dal.get_agent_inbox(agent_id="main", type_filter=BENCHMARK_DIGEST_TYPE, limit=10)
        sql, params = captured[0]
        assert "NOT IN" not in sql.upper() or BENCHMARK_DIGEST_TYPE in str(params)


# ─── 7. The wiring: something must SET the marker ────────────────────────────


class TestTheMarkerIsActuallySet:
    """Every other test in this file establishes the marker itself.

    So all of them prove the boundary works GIVEN the marker, and none prove
    anything sets it. A mutation probe confirmed the gap: neutering
    ``mark_benchmark_run`` in the runner left 894 tests passing and zero red.
    This repo has recorded that exact failure at least six times — "PROBE,
    don't trust silence"; "correct function, inert CALLER".
    """

    def test_mark_benchmark_run_sets_and_clears_the_marker(self) -> None:
        from robothor.engine.run_context import (
            current_benchmark_run,
            in_benchmark_run,
            mark_benchmark_run,
        )

        session = SimpleNamespace(run=SimpleNamespace(is_benchmark=False), run_id="run-9")
        config = SimpleNamespace(is_benchmark=True)

        mark_benchmark_run(session, config, "agent-under-test")
        try:
            assert session.run.is_benchmark is True
            marker = current_benchmark_run()
            assert marker is not None
            assert marker.agent_id == "agent-under-test"
            assert marker.run_id == "run-9"

            # An ordinary run must CLEAR it, not leave the previous one standing.
            other = SimpleNamespace(run=SimpleNamespace(is_benchmark=False), run_id="run-10")
            mark_benchmark_run(other, SimpleNamespace(is_benchmark=False), "another-agent")
            assert other.run.is_benchmark is False
            assert in_benchmark_run() is False
        finally:
            from robothor.engine.run_context import set_benchmark_run

            set_benchmark_run(None)

    @pytest.mark.asyncio
    async def test_the_harness_binds_the_marker_around_the_child_run(self) -> None:
        """``_execute_task_run`` with ``seeded=None`` — the branch the incident
        went through — must bind the marker before ``runner.execute`` and
        restore it afterwards."""
        from robothor.engine.run_context import in_benchmark_run
        from robothor.engine.tools.handlers.benchmark import _execute_task_run

        seen: dict[str, Any] = {}

        class _Runner:
            async def execute(self, **kwargs: Any) -> str:
                seen["inside"] = in_benchmark_run()
                seen["trigger_detail"] = kwargs["trigger_detail"]
                return "ran"

        assert in_benchmark_run() is False
        result = await _execute_task_run(
            runner=_Runner(),
            agent_id="agent-under-test",
            prompt="p",
            trigger_detail="benchmark:suite:1",
            child_config=SimpleNamespace(is_benchmark=True),
            spawn_context=None,
            seeded=None,
        )

        assert result == "ran"
        assert seen["inside"] is True, "the harness did not bind the marker for the child run"
        assert seen["trigger_detail"].startswith("benchmark:")
        assert in_benchmark_run() is False, "the marker leaked past the child run"

    @pytest.mark.asyncio
    async def test_a_write_attempted_from_inside_a_harness_child_is_refused(
        self, monkeypatch: Any
    ) -> None:
        """End to end through the real wiring: nothing in this test establishes
        the marker, so it fails if the harness stops binding it.

        ``get_connection`` is patched to raise rather than left alone: without
        the guard this test would otherwise reach the box's real database and
        the failure would be a ForeignKeyViolation from Postgres. A test that
        writes to production to prove writes to production are blocked is not a
        test anyone should run twice.
        """
        from robothor.engine.tools.handlers.benchmark import _execute_task_run
        from robothor.memory import facts as facts_mod

        def _no_database(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("a benchmark child's write reached the database")

        monkeypatch.setattr(facts_mod, "get_connection", _no_database)
        monkeypatch.setattr(
            facts_mod.llm_client,
            "get_embeddings_batch_async",
            AsyncMock(return_value=[[0.0]]),
            raising=False,
        )

        stored: list[Any] = []

        class _Runner:
            async def execute(self, **_kwargs: Any) -> str:
                stored.append(
                    await facts_mod.store_facts_batch(
                        [{"fact_text": "a fixture person", "category": "personal"}],
                        "a fixture person",
                        "conversation",
                        tenant_id=_PRODUCTION,
                    )
                )
                return "ran"

        await _execute_task_run(
            runner=_Runner(),
            agent_id="agent-under-test",
            prompt="p",
            trigger_detail="benchmark:suite:1",
            child_config=SimpleNamespace(is_benchmark=True),
            spawn_context=None,
            seeded=None,
        )
        assert stored == [[]], "a benchmark child's memory write was not refused"


# ─── The marker must survive the hops the platform actually takes ────────────


class TestMarkerPropagation:
    @pytest.mark.asyncio
    async def test_it_survives_to_thread_and_create_task(self) -> None:
        from robothor.engine.run_context import in_benchmark_run

        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            assert await asyncio.to_thread(in_benchmark_run) is True
            assert await asyncio.create_task(_in_run()) is True

    @pytest.mark.asyncio
    async def test_it_survives_the_hook_executor(self) -> None:
        """``hook_registry`` runs sync handlers through ``run_in_executor``,
        which does NOT copy the context. ``buddy_hooks._on_agent_end`` is a
        sync handler registered globally on AGENT_END, so it takes this hop on
        every run — including a graded one."""
        from robothor.engine.hook_registry import HookRegistry

        registry = HookRegistry()
        with benchmark_run_scope(True, agent_id="a", run_id="r"):
            assert await registry._run_in_executor(in_benchmark_run_probe) is True


def in_benchmark_run_probe() -> bool:
    from robothor.engine.run_context import in_benchmark_run

    return in_benchmark_run()


async def _in_run() -> bool:
    from robothor.engine.run_context import in_benchmark_run

    return in_benchmark_run()
