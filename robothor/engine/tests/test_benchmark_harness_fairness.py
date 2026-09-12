"""The benchmark harness must not fail an agent for the harness's own limits.

Three defects, all of which produce a low grade that says nothing about the
agent:

A. The sub-agent tool allow-list was hand-maintained and had drifted. Pure
   reads that agents' documented procedures depend on were stripped
   (``get_stats``, ``list_agent_reviews``, ``devops_query_metrics`` …) while
   ``create_goal`` — a write the registry force-adds after the manifest
   filter — was handed to every benchmark sub-agent.

B. The LLM judge saw ``output[:3000]``. A 5000-char answer reached the grader
   without its conclusion, and a 4-item rubric at a 0.7 threshold fails the
   whole case on one rubric item that lived in the missing tail.

C. A hardcoded 240s per-task cap against a production fleet that runs with
   ``timeout_seconds: 0``. Worse, the kill was scored: the runner absorbs the
   cancellation and returns a TIMEOUT run with empty output, so the vacuous
   ``must_not_contain`` checks passed and the case was filed as partial credit
   on a wrong answer rather than as a harness timeout.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

CTX = ToolContext(agent_id="auto-agent", workspace="/tmp/test-workspace")


# ─── Helpers (mirrors of test_benchmark.py's, kept local for isolation) ──


def _mock_blocks():
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-04-03T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _block_patches(read_fn, write_fn):
    return (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
    )


def _make_mock_run(output_text="ok", cost=0.01, steps=2, status="completed"):
    run = MagicMock()
    run.output_text = output_text
    run.total_cost_usd = cost
    run.steps = [MagicMock()] * steps
    run.status = MagicMock(value=status)
    run.id = "run-abc"
    run.error_message = None
    return run


def _agent_cfg(tools_allowed: list[str]):
    cfg = MagicMock()
    cfg.max_iterations = 10
    cfg.cost_budget_usd = 1.0
    cfg.tools_allowed = list(tools_allowed)
    cfg.tools_denied = []
    cfg.is_benchmark = False
    return cfg


@pytest.fixture(autouse=True)
def _isolate_benchmark_results_db(monkeypatch):
    """Never let these tests write to a real ``benchmark_results`` table."""

    class _FakeCursor:
        def execute(self, *a, **kw):
            return None

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def get_dsn_parameters(self):
            return {"dbname": "robothor_test"}

        def cursor(self, *args, **kwargs):
            # Tolerate cursor_factory=… so a stray patch can never turn into a
            # confusing TypeError in an unrelated module.
            return _FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import robothor.db.connection as _conn_mod

    def _fake_get_connection(*a, **kw):
        return _FakeConn()

    # Claim the real function's module identity. ``tests/conftest_integration``
    # sweeps ``sys.modules`` for stragglers by ``__module__`` and rebinds them
    # to its proxy; without this, a module that happens to import
    # ``get_connection`` for the first time DURING one of these tests keeps the
    # fake for the rest of the session.
    _fake_get_connection.__module__ = "robothor.db.connection"
    monkeypatch.setattr(_conn_mod, "get_connection", _fake_get_connection)


async def _run_suite(suite: dict[str, Any], mock_runner, agent_cfg) -> dict[str, Any]:
    store, read_fn, write_fn = _mock_blocks()
    store[f"benchmark:{suite['agent_id']}:{suite['id']}"] = json.dumps(suite)
    p1, p2 = _block_patches(read_fn, write_fn)
    from robothor.engine.tools.handlers.benchmark import _benchmark_run

    with (
        p1,
        p2,
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=mock_runner),
        patch("robothor.engine.config.load_agent_config", return_value=agent_cfg),
    ):
        return await _benchmark_run(
            {"agent_id": suite["agent_id"], "suite_id": suite["id"], "tag": "t"}, CTX
        )


def _runner(execute) -> MagicMock:
    runner = MagicMock()
    runner.execute = execute
    runner.config = MagicMock()
    runner.config.manifest_dir = "/tmp"
    return runner


# ═══ A. The tool allow-list ══════════════════════════════════════════════


class TestReadOnlyToolsReachTheAgent:
    """Pure reads an agent's documented procedure needs must survive the harness."""

    @pytest.mark.parametrize(
        ("tool", "why"),
        [
            ("get_knowledge_gaps", "curiosity-engine step 1 of 7"),
            ("get_stats", "curiosity-engine step 1 of 7"),
            ("list_agent_reviews", "agent-architect must cite a review_id"),
            ("get_agent_review", "agent-architect must cite a review_id"),
            ("get_fleet_achievement_score", "agent-architect fleet triage"),
            ("experiment_status", "agent-architect dispatch check"),
            ("devops_query_metrics", "devops-analyst trend analysis"),
            ("render_devops_report", "devops-analyst report shape self-check"),
        ],
    )
    def test_pure_read_is_not_stripped(self, tool: str, why: str):
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        denied = _benchmark_tools_denied([tool, "read_file"])
        assert tool not in denied, f"benchmark harness strips read-only {tool} ({why})"

    def test_write_tool_is_still_stripped(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        denied = set(_benchmark_tools_denied(["exec", "gws_gmail_send", "delete_person"]))
        assert {"exec", "gws_gmail_send", "delete_person"} <= denied

    def test_receive_agent_messages_is_not_read_only(self):
        """``messenger.receive`` is ``rpop`` — a destructive read.

        It was classified in ``READONLY_TOOLS``, which plan mode trusts and
        (since this change) the benchmark allow-list derives from. A benchmark
        sub-run of an agent would have drained that agent's real Redis inbox,
        and plan mode — whose whole promise is "look, don't touch" — would too.
        """
        from robothor.engine.tools.constants import READONLY_TOOLS
        from robothor.engine.tools.handlers.benchmark import (
            _benchmark_tools_denied,
            benchmark_readonly_tools,
        )

        assert "receive_agent_messages" not in READONLY_TOOLS
        assert "receive_agent_messages" not in benchmark_readonly_tools()
        assert "receive_agent_messages" in set(
            _benchmark_tools_denied(["receive_agent_messages", "read_file"])
        )

    def test_goal_writes_denied_even_when_the_manifest_never_declared_them(self):
        """``create_goal``/``update_goal`` are force-added by the registry.

        ``ToolRegistry._get_filtered_names`` appends GOAL_TOOLS *after*
        intersecting ``tools_allowed``, so a benchmark sub-agent whose manifest
        never asked for them still gets them unless they are named explicitly
        in ``tools_denied``. Production transcripts show benchmark sub-runs of
        agent-architect calling ``update_goal`` — a durable write made by an
        agent that was supposed to be read-only.
        """
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        denied = set(_benchmark_tools_denied(["read_file", "search_memory"]))
        assert "create_goal" in denied
        assert "update_goal" in denied
        assert "get_goal" not in denied, "reading the goal has no side effect"


class TestToolClassificationParity:
    """A newly registered tool must be classified, not silently defaulted."""

    def test_every_registered_tool_is_classified(self):
        from robothor.api.mcp import get_tool_definitions
        from robothor.engine.benchmark_sandbox import (
            EXTERNAL_SIDE_EFFECT_TOOLS,
            SANDBOX_WRITE_TOOLS,
            benchmark_allowed_tools,
        )
        from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS
        from robothor.engine.tools.schemas import get_engine_schemas

        registered = {d["name"] for d in get_tool_definitions()} | set(get_engine_schemas())
        classified = (
            benchmark_allowed_tools(sandbox=True)
            | SANDBOX_WRITE_TOOLS
            | EXTERNAL_SIDE_EFFECT_TOOLS
            | _BENCHMARK_EXCLUDED_TOOLS
        )
        unclassified = sorted(registered - classified)
        assert not unclassified, (
            "these tools are neither benchmark-allowed nor deliberately excluded — "
            "classify them in robothor/engine/tools/constants.py (READONLY_TOOLS) or "
            f"_BENCHMARK_EXCLUDED_TOOLS: {unclassified}"
        )

    def test_a_runtime_registered_adapter_tool_is_classified_as_denied(self, monkeypatch):
        """The classification universe must include what arrives at RUNTIME.

        ``get_tool_definitions()`` and ``get_engine_schemas()`` are static, so
        an adapter tool registered from a server's ``tools/list`` was in
        neither and this parity test could not see it. It is not merely
        unclassified: it is dispatched at ``dispatch._execute_tool`` before
        ``ToolContext`` exists, so no ``ctx.is_benchmark`` gate runs on it.
        """
        from robothor.engine.tools import get_registry
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        name = "acme_delete_patient"
        registry = get_registry()
        monkeypatch.setitem(
            registry._schemas,
            name,
            {"type": "function", "function": {"name": name, "parameters": {}}},
        )
        monkeypatch.setitem(registry._adapter_routes, name, "acme")

        assert name in registry.registered_tool_names()
        assert name in _benchmark_tools_denied(None, sandbox=False)
        assert name in _benchmark_tools_denied([name], sandbox=True)

    def test_excluded_and_allowed_do_not_overlap(self):
        """Since 2026-09-10 ``benchmark_readonly_tools()`` subtracts
        ``_BENCHMARK_EXCLUDED_TOOLS`` itself, so the read-only half of this can
        no longer fail. The bite that remains is ``sandbox=True``: the sandbox
        write set is unioned in AFTER that subtraction, so this still catches
        the real contradiction — a tool listed both as a sandbox write and as
        deliberately excluded.
        """
        from robothor.engine.benchmark_sandbox import SANDBOX_WRITE_TOOLS, benchmark_allowed_tools
        from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS

        overlap = sorted(benchmark_allowed_tools(sandbox=True) & _BENCHMARK_EXCLUDED_TOOLS)
        assert not overlap, f"tool both allowed and excluded: {overlap}"
        assert not sorted(SANDBOX_WRITE_TOOLS & _BENCHMARK_EXCLUDED_TOOLS), (
            "a sandbox write tool is also named in _BENCHMARK_EXCLUDED_TOOLS"
        )

    def test_allow_list_is_derived_not_hand_copied(self):
        """The benchmark allow-list must be a superset of the shared read-only set.

        Minus what the benchmark deliberately withholds. A hand-maintained
        second copy is what rotted the first time.
        """
        from robothor.engine.tools.constants import READONLY_TOOLS
        from robothor.engine.tools.handlers.benchmark import (
            _BENCHMARK_WITHHELD_READS,
            benchmark_readonly_tools,
        )

        missing = sorted(READONLY_TOOLS - _BENCHMARK_WITHHELD_READS - benchmark_readonly_tools())
        assert not missing, f"read-only tools missing from the benchmark allow-list: {missing}"

    # ── Adapter tools: `read_only:` is the only way in ────────────────
    #
    # Adapter tools are registered from an MCP server's tools/list, so they
    # have no static schema and never reach
    # ``test_every_registered_tool_is_classified``. Core's three deny-sets are
    # all literal core tool names, so none of them can see
    # ``acme_delete_patient``. If mere presence in ``tools_allowed`` were
    # enough, that tool would reach a graded sub-agent the moment the
    # benchmarked agent's manifest granted it — the 2026-05-28 boundary again.

    @staticmethod
    def _adapter(tmp_path, body: str, name: str = "acme") -> None:
        (tmp_path / f"{name}.yaml").write_text(
            f"name: {name}\ntransport: http\nurl: https://api.example.com/_mcp\n"
            f"agents: ['*']\n{body}"
        )

    def test_only_declared_read_only_adapter_tools_enter_the_allow_list(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``read_only`` classifies; ``tools_allowed`` only bounds reach.

        Until 2026-09-10 four of one operator's adapter tool names were typed
        into the allow-list by hand, so core shipped a stranger's vendor and
        every OTHER instance's adapters were silently denied in benchmarks.
        Deriving the replacement from ``tools_allowed`` would have been worse
        than the hardcoding: a write tool nobody classified, handed to the
        agent being graded.
        """
        from robothor.engine import adapters
        from robothor.engine.tools.handlers.benchmark import benchmark_readonly_tools

        # Empty the cache FIRST: a dev box with real adapters installed must
        # not change what this test measures.
        monkeypatch.setattr(adapters, "_loaded_adapters", [])
        before = benchmark_readonly_tools()

        self._adapter(
            tmp_path,
            "tools_allowed: [x_get, x_delete]\nread_only: [x_get]\n",
        )
        monkeypatch.setattr(adapters, "_loaded_adapters", adapters.load_adapters(tmp_path))

        assert benchmark_readonly_tools() - before == {"x_get"}, (
            "an undeclared adapter tool must never be treated as read-only"
        )

    def test_an_adapter_that_declares_nothing_contributes_nothing(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent means WRITE — the plugin seam's rule, not a lenient default."""
        from robothor.engine import adapters
        from robothor.engine.tools.handlers.benchmark import benchmark_readonly_tools

        monkeypatch.setattr(adapters, "_loaded_adapters", [])
        before = benchmark_readonly_tools()

        self._adapter(tmp_path, "tools_allowed: [x_get, x_delete]\n")
        monkeypatch.setattr(adapters, "_loaded_adapters", adapters.load_adapters(tmp_path))

        assert benchmark_readonly_tools() == before

    def test_read_only_outside_tools_allowed_refuses_the_adapter(self, tmp_path) -> None:
        """Classifying a tool it does not serve is escalation, not extension.

        Refused at LOAD, not ignored at use: a classification that is silently
        dropped leaves the operator believing a boundary exists that nothing
        enforces.
        """
        from robothor.engine import adapters

        self._adapter(tmp_path, "tools_allowed: [x_get]\nread_only: [x_get, read_file]\n")
        assert adapters.load_adapters(tmp_path) == []

    @pytest.mark.parametrize("value", ["x_get", "false", "0", "{a: 1}"])
    def test_a_non_list_read_only_refuses_the_adapter(self, tmp_path, value: str) -> None:
        """Including the falsy scalars. ``or []`` would have read
        ``read_only: false`` as "declared nothing" and loaded the adapter
        anyway — the fail-open shape command_sha256 was already bitten by."""
        from robothor.engine import adapters

        self._adapter(tmp_path, f"tools_allowed: [x_get]\nread_only: {value}\n")
        assert adapters.load_adapters(tmp_path) == []

    def test_read_only_beside_a_legacy_allow_all_is_refused(self, tmp_path) -> None:
        """Empty ``tools_allowed`` is legacy allow-all, so it grounds no claim:
        there is nothing for the subset check to check against."""
        from robothor.engine import adapters

        self._adapter(tmp_path, "read_only: [x_get]\n")
        assert adapters.load_adapters(tmp_path) == []

    def test_an_adapter_cannot_reopen_a_withheld_tool(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``read_only`` is operator-declared, so it must not be trusted to
        widen the harness's own withheld set. Withholding is subtracted last.

        BELT 1 IS DISABLED HERE ON PURPOSE. Every name in
        ``_BENCHMARK_WITHHELD_READS`` is a core-registry name, so belt 1 would
        refuse this fixture at load and the test would pass without the
        adapter ever reaching the allow-list — certifying core's own
        subtraction while its name promised something about operator-declared
        ``read_only``. Stubbing ``_core_tool_names`` empty puts the greedy
        adapter into the loaded set, which is what belt 2 has to survive.
        """
        from robothor.engine import adapters
        from robothor.engine.benchmark_sandbox import benchmark_allowed_tools
        from robothor.engine.tools.handlers.benchmark import (
            _BENCHMARK_WITHHELD_READS,
            benchmark_readonly_tools,
        )

        monkeypatch.setattr(adapters, "_core_tool_names", lambda: frozenset())
        withheld = sorted(_BENCHMARK_WITHHELD_READS)[0]
        self._adapter(
            tmp_path,
            f"tools_allowed: ['{withheld}']\nread_only: ['{withheld}']\n",
            name="greedy",
        )
        loaded = adapters.load_adapters(tmp_path)
        assert loaded, "belt 1 must be off for this test to test belt 2"
        assert withheld in loaded[0].read_only

        monkeypatch.setattr(adapters, "_loaded_adapters", loaded)
        assert withheld not in benchmark_readonly_tools()
        for sandbox in (False, True):
            assert withheld not in benchmark_allowed_tools(sandbox=sandbox), (
                f"an adapter re-opened a withheld tool (sandbox={sandbox})"
            )

    # ── Two belts, each tested with the other off ─────────────────────
    #
    # Belt 1 refuses an adapter that NAMES a core tool at all (adapters.py).
    # Belt 2 subtracts `_BENCHMARK_EXCLUDED_TOOLS` from the allow-list at
    # runtime (benchmark.py). Either alone closes the hole, so each is probed
    # with the other disabled — otherwise one could rot silently behind the
    # other and nothing would say so.

    CORE_NAMES_AN_ADAPTER_MIGHT_CLAIM = "[delete_person, git_push, create_pull_request]"

    def test_an_adapter_naming_core_tools_is_refused(self, tmp_path) -> None:
        """BELT 1. `tools_allowed` bounds an adapter's OWN server, so a core
        name in it is a claim over something the adapter does not serve.

        Left open, an adapter YAML was a way to reclassify core's write tools
        as read-only: `read_only ⊆ tools_allowed` holds perfectly when both
        say `delete_person`.
        """
        from robothor.engine import adapters

        claim = self.CORE_NAMES_AN_ADAPTER_MIGHT_CLAIM
        self._adapter(tmp_path, f"tools_allowed: {claim}\nread_only: {claim}\n", name="thief")
        assert adapters.load_adapters(tmp_path) == []

    @pytest.mark.parametrize("declared_via", ["tools", "schemas"])
    def test_a_plugin_contributed_name_is_refused_too(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, declared_via: str
    ) -> None:
        """BELT 1 covers plugins, not just core.

        A plugin's tools are no more an adapter's to classify than core's are,
        and a plugin already declares its own ``read_only`` through its own
        seam. Both keys of the loader result count: a plugin shipping only a
        handler still gets a synthesized schema and is advertised to the model
        (``ToolRegistry._register_plugin_schemas``), so checking ``schemas``
        alone would leave the same hole one layer down.
        """
        import robothor.plugins as plugins_mod
        from robothor.engine import adapters

        fake = SimpleNamespace(tools={}, schemas={}, read_only=set(), failures=[], loaded=[])
        setattr(fake, declared_via, {"plugin_write_thing": object()})
        monkeypatch.setattr(plugins_mod, "load_plugins", lambda *_a, **_k: fake)

        self._adapter(
            tmp_path,
            "tools_allowed: [plugin_write_thing]\nread_only: [plugin_write_thing]\n",
            name="thief",
        )
        assert adapters.load_adapters(tmp_path) == [], (
            f"an adapter claimed a plugin tool declared via {declared_via}"
        )

    def test_an_unreadable_name_source_refuses_the_adapter(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed. "Could not check" must never degrade to "allowed" —
        the rule verify_adapter_integrity already follows for an unverifiable
        pin. A broken plugin package is the realistic way this happens.
        """
        from robothor.engine import adapters

        def _boom() -> frozenset[str]:
            raise RuntimeError("plugin entry point exploded")

        monkeypatch.setattr(adapters, "_core_tool_names", _boom)
        self._adapter(tmp_path, "tools_allowed: [x_get]\nread_only: [x_get]\n")
        assert adapters.load_adapters(tmp_path) == []

    def test_an_unreadable_name_source_does_not_refuse_a_declarationless_adapter(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check is skipped when there is nothing to check, so a legacy
        allow-all adapter is not collateral damage of a broken plugin."""
        from robothor.engine import adapters

        def _boom() -> frozenset[str]:
            raise RuntimeError("plugin entry point exploded")

        monkeypatch.setattr(adapters, "_core_tool_names", _boom)
        self._adapter(tmp_path, "description: legacy allow-all\n")
        assert len(adapters.load_adapters(tmp_path)) == 1

    def test_core_write_tools_stay_out_even_with_the_load_check_disabled(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BELT 2, with belt 1 monkeypatched off.

        `_core_tool_names` is stubbed empty so the adapter loads with core
        names in both lists — the exact state belt 1 exists to prevent. The
        runtime subtraction must still keep them out of BOTH the read-only
        baseline and `benchmark_allowed_tools()`, sandboxed or not.
        """
        from robothor.engine import adapters
        from robothor.engine.benchmark_sandbox import benchmark_allowed_tools
        from robothor.engine.tools.handlers.benchmark import benchmark_readonly_tools

        monkeypatch.setattr(adapters, "_core_tool_names", lambda: frozenset())
        claim = self.CORE_NAMES_AN_ADAPTER_MIGHT_CLAIM
        self._adapter(tmp_path, f"tools_allowed: {claim}\nread_only: {claim}\n", name="thief")
        loaded = adapters.load_adapters(tmp_path)
        assert loaded, "belt 1 must be off for this test to test belt 2"

        monkeypatch.setattr(adapters, "_loaded_adapters", loaded)
        smuggled = {"delete_person", "git_push", "create_pull_request"}
        assert not smuggled & benchmark_readonly_tools()
        for sandbox in (False, True):
            assert not smuggled & benchmark_allowed_tools(sandbox=sandbox), (
                f"a core write tool reached a graded sub-agent (sandbox={sandbox})"
            )


# ═══ B. The judge window ═════════════════════════════════════════════════


class TestJudgeSeesTheWholeAnswer:
    async def _capture_judge_prompt(self, output: str) -> str:
        from robothor.engine.tools.handlers.benchmark import _judge_output

        captured: dict[str, str] = {}

        async def _fake_acompletion(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = '{"scores": [1]}'
            return resp

        with patch("litellm.acompletion", side_effect=_fake_acompletion):
            await _judge_output(output, ["says something"], "test/model")
        return captured["prompt"]

    @pytest.mark.asyncio
    async def test_five_thousand_char_output_reaches_the_judge_intact(self):
        """The 8 worst-affected cases average ~5.2K chars. All of it must arrive."""
        output = "OPENING-MARKER\n" + ("filler line\n" * 400) + "\nCONCLUSION-MARKER"
        assert 3000 < len(output) < 12000
        prompt = await self._capture_judge_prompt(output)
        assert "OPENING-MARKER" in prompt
        assert "CONCLUSION-MARKER" in prompt, (
            "the judge never saw the conclusion — rubric items about the "
            "recommendation fail on a correct answer"
        )

    @pytest.mark.asyncio
    async def test_oversized_output_keeps_head_and_tail(self):
        """Past the window, keep both ends: conclusions live at the end."""
        output = "OPENING-MARKER\n" + ("x" * 200000) + "\nCONCLUSION-MARKER"
        prompt = await self._capture_judge_prompt(output)
        assert "OPENING-MARKER" in prompt
        assert "CONCLUSION-MARKER" in prompt
        assert "omitted" in prompt.lower(), "elision must be marked so the judge knows"
        assert len(prompt) < 40000, "the window must still bound judge cost"

    def test_window_is_a_named_constant(self):
        from robothor.engine.tools.handlers.benchmark import _JUDGE_OUTPUT_CHARS

        assert _JUDGE_OUTPUT_CHARS >= 12000


# ═══ C. The per-task wall-clock cap ══════════════════════════════════════


def _timeout_suite(**suite_extra: Any) -> dict[str, Any]:
    suite = {
        "id": "s-timeout",
        "agent_id": "main",
        "max_cost_usd": 1.0,
        "tasks": [
            {
                "id": "slow-task",
                "prompt": "think hard",
                "category": "correctness",
                "weight": 1.0,
                "expected": {"must_contain": ["alpha"], "must_not_contain": ["beta"]},
            }
        ],
    }
    suite.update(suite_extra)
    return suite


class TestTimeoutIsADistinctOutcome:
    @pytest.mark.asyncio
    async def test_per_task_cap_is_configurable_per_suite(self):
        async def _slow(**kwargs):
            await asyncio.sleep(3)
            return _make_mock_run(output_text="alpha")

        result = await _run_suite(
            _timeout_suite(task_timeout_seconds=0.05),
            _runner(AsyncMock(side_effect=_slow)),
            _agent_cfg(["read_file"]),
        )
        task = result["task_results"][0]
        assert task.get("timed_out") is True, (
            "a suite-level task_timeout_seconds must be honoured — the 240s "
            "hardcode has no relationship to how these agents run in production"
        )

    @pytest.mark.asyncio
    async def test_task_level_cap_overrides_the_suite(self):
        async def _slow(**kwargs):
            await asyncio.sleep(3)
            return _make_mock_run(output_text="alpha")

        suite = _timeout_suite(task_timeout_seconds=600)
        suite["tasks"][0]["timeout_seconds"] = 0.05
        result = await _run_suite(
            suite, _runner(AsyncMock(side_effect=_slow)), _agent_cfg(["read_file"])
        )
        assert result["task_results"][0].get("timed_out") is True

    @pytest.mark.asyncio
    async def test_timeout_is_not_scored_as_a_wrong_answer(self):
        """A harness kill must not become partial credit on vacuous checks.

        The runner absorbs the cancellation and returns a TIMEOUT run with an
        empty ``output_text``. Every ``must_not_contain`` pattern then passes
        against the empty string, so the case was recorded at 0.5 — a grade
        that reads as "the agent half-answered" when the agent was killed
        mid-thought.
        """
        result = await _run_suite(
            _timeout_suite(),
            _runner(AsyncMock(return_value=_make_mock_run(output_text="", status="timeout"))),
            _agent_cfg(["read_file"]),
        )
        task = result["task_results"][0]
        assert task.get("timed_out") is True
        assert task["outcome"] == "timeout"
        assert task["score"] == 0.0, (
            f"a harness timeout scored {task['score']} from vacuous checks on empty output"
        )

    @pytest.mark.asyncio
    async def test_run_record_counts_timeouts_separately(self):
        result = await _run_suite(
            _timeout_suite(),
            _runner(AsyncMock(return_value=_make_mock_run(output_text="", status="timeout"))),
            _agent_cfg(["read_file"]),
        )
        assert result["timeouts"] == 1
        assert result["passed"] == 0
        assert result["total_cases"] == 1, "a timeout stays in the denominator"

    @pytest.mark.asyncio
    async def test_scored_failure_is_not_labelled_a_timeout(self):
        result = await _run_suite(
            _timeout_suite(),
            _runner(AsyncMock(return_value=_make_mock_run(output_text="gamma"))),
            _agent_cfg(["read_file"]),
        )
        task = result["task_results"][0]
        assert not task.get("timed_out")
        assert task["outcome"] == "scored"
        assert result["timeouts"] == 0
        assert task["score"] == 0.5

    def test_default_cap_reflects_how_these_agents_actually_run(self):
        """agent-architect's production runs mean 512.8s and max 728.5s, with
        zero production timeouts — ``_defaults.yaml`` sets ``timeout_seconds: 0``.
        A 240s harness cap sits inside that distribution."""
        from robothor.engine.tools.handlers.benchmark import _DEFAULT_TASK_TIMEOUT_SECONDS

        assert _DEFAULT_TASK_TIMEOUT_SECONDS >= 750
