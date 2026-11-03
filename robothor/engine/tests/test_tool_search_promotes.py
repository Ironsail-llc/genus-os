"""A ``tool_search`` hit becomes a real tool for the rest of the run.

On a deferred run every non-core tool used to be reachable only through
``tool_call``, whose declared schema is ``arguments: {"type": "object"}`` with
no properties. Some models do not manage that free-form nested call: one wrote
multilingual token salad with JSON shards into the arguments instead. So the
top hits of a search (at most three, from the run's own allow-set) join the
advertised toolset with their REAL schemas, and the next model call carries
them: the model calls ``gws_gmail_search`` with ``gws_gmail_search``'s
parameters. ``tool_call`` stays as the fallback and ``tool_describe`` is
unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from robothor.engine.runner import AgentRunner
from robothor.engine.stall_watchdog import _StallWatchdog
from robothor.engine.tools.constants import TOOLSEARCH_TOOLS
from robothor.engine.tools.registry import ToolRegistry, wire_schema

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS"):
        monkeypatch.delenv(name, raising=False)


def _config(manifest: dict[str, Any]):
    from robothor.engine.config import manifest_to_agent_config

    cfg = manifest_to_agent_config(manifest)
    cfg.planning_enabled = False
    cfg.scratchpad_enabled = False
    cfg.error_feedback = False
    cfg.timeout_seconds = 60
    return cfg


def _broad(registry: ToolRegistry, *, drop: tuple[str, ...] = ()):
    allowed = [n for n in sorted(registry._schemas) if n not in TOOLSEARCH_TOOLS and n not in drop]
    return _config({"id": "broad", "name": "broad", "tools_allowed": allowed})


def _main():
    text = (REPO / "templates/agents/core/main/manifest.template.yaml").read_text()
    return _config(yaml.safe_load(text.replace("{{ timezone }}", "UTC")))


def _response(content=None, tool_calls=None):
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


def _call(i: int, name: str, arguments: dict[str, Any]):
    call = MagicMock()
    call.id = f"call_{i}"
    call.function.name = name
    call.function.arguments = json.dumps(arguments)
    return call


async def _drive(
    cfg, registry: ToolRegistry, calls: list[tuple[str, dict[str, Any]]], engine_config
):
    """Run ``cfg`` with a model that makes ``calls`` in turn, then answers.

    Returns the tool schemas each model call carried, and every tool result.
    """
    from robothor.engine.session import AgentSession

    seen: list[list[dict[str, Any]]] = []
    results: list[dict[str, Any]] = []

    async def llm(**kw):
        if not kw.get("tools"):
            # Not the loop's call: a side call (a classifier, a summary) or the
            # text-only final turn. Neither is what this file is about.
            return _response(content="done")
        seen.append(list(kw["tools"]))
        for message in kw.get("messages") or []:
            if message.get("role") == "tool":
                with contextlib.suppress(TypeError, ValueError):
                    results.append(json.loads(message.get("content") or "{}"))
        i = len(seen) - 1
        if i < len(calls):
            name, args = calls[i]
            return _response(tool_calls=[_call(i, name, args)])
        return _response(content="done")

    async def no_recent(*_a, **_k):
        return frozenset()

    with (
        patch("robothor.engine.runner.get_registry", return_value=registry),
        patch("robothor.engine.tools.registry.get_registry", return_value=registry),
        patch("robothor.engine.recent_tools.recent_tool_names", no_recent),
        patch("asyncio.timeout", lambda *_: contextlib.nullcontext()),
        patch.object(_StallWatchdog, "start", lambda self, task: None),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("robothor.engine.run_finalizer.update_run"),
        patch.object(AgentSession, "flush_new_steps_sync", lambda self: 0),
        patch("litellm.acompletion", side_effect=llm),
        # The meta-tools are not journalled reads, and this file has no effect
        # database: dispatch straight through, as a benchmark run does.
        patch("robothor.engine.runtime.effect_dispatch._bypass", return_value=True),
    ):
        runner = AgentRunner(engine_config)
        runner.registry = registry
        await asyncio.wait_for(runner.execute(cfg.id, "go", agent_config=cfg), timeout=60)
    return seen, results


def _by_name(schemas: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["function"]["name"]: s for s in schemas if "function" in s}


def _search_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    return next(r for r in results if "results" in r and "hint" in r)


class TestASearchHitIsPromoted:
    @pytest.mark.asyncio
    async def test_the_next_call_carries_the_hits_real_schemas(self, engine_config) -> None:
        registry = ToolRegistry()
        cfg = _broad(registry)
        seen, results = await _drive(
            cfg, registry, [("tool_search", {"query": "search my email"})], engine_config
        )

        before, after = _by_name(seen[0]), _by_name(seen[1])
        assert "gws_gmail_search" not in before, "precondition: it was deferred"
        expected = wire_schema(registry.get_schema("gws_gmail_search"))
        assert after["gws_gmail_search"] == expected
        assert after["gws_gmail_search"]["function"]["parameters"]["properties"]

        promoted = _search_result(results)["promoted"]
        assert "gws_gmail_search" in promoted
        assert set(after) - set(before) == set(promoted)
        assert len(after) == len(before) + len(promoted)

    @pytest.mark.asyncio
    async def test_at_most_three_hits_are_promoted(self, engine_config) -> None:
        registry = ToolRegistry()
        cfg = _broad(registry)
        seen, results = await _drive(
            cfg, registry, [("tool_search", {"query": "email", "limit": 10})], engine_config
        )

        promoted = _search_result(results)["promoted"]
        assert 0 < len(promoted) <= 3
        assert len(seen[1]) - len(seen[0]) == len(promoted)

    @pytest.mark.asyncio
    async def test_a_tool_outside_the_allow_set_is_never_promoted(self, engine_config) -> None:
        registry = ToolRegistry()
        cfg = _broad(registry, drop=("gws_gmail_search",))
        seen, results = await _drive(
            cfg, registry, [("tool_search", {"query": "search my email"})], engine_config
        )

        assert "gws_gmail_search" not in _by_name(seen[1])
        assert "gws_gmail_search" not in _search_result(results)["promoted"]
        allowed = set(registry.get_tool_names(cfg))
        assert set(_by_name(seen[1])) - set(_by_name(seen[0])) <= allowed

    def test_a_name_marked_outside_the_allow_set_is_refused_by_the_runner(self) -> None:
        """Defence in depth: whatever marks a name, only the allow-set reaches the wire."""
        from robothor.engine.toolset_prep import with_promoted

        registry = ToolRegistry()
        cfg = _broad(registry, drop=("exec",))
        base = registry.build_for_agent(cfg, recent=frozenset())
        out = with_promoted(registry, cfg, base, {"exec", "gws_gmail_search"})
        names = _by_name(out)
        assert "exec" not in names
        assert "gws_gmail_search" in names


class TestARunWithoutSearchesIsUnchanged:
    @pytest.mark.asyncio
    async def test_main_keeps_its_core_on_every_call(self, engine_config) -> None:
        registry = ToolRegistry()
        seen, _results = await _drive(
            _main(),
            registry,
            [("tool_describe", {"name": "gws_calendar_list"})],
            engine_config,
        )

        assert len(seen) == 2
        assert [len(s) for s in seen] == [34, 34]
        assert _by_name(seen[0]) == _by_name(seen[1])
