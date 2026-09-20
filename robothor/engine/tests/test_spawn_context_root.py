"""The root spawn context a real run builds, not one a test hand-assembled.

Every other spawn test constructs ``SpawnContext(...)`` directly, so all of
them exercised the dataclass default (``fleet_release_id = None``) and none of
them exercised ``make_spawn_context``, which is the single production root
constructor (``runner.py``). ``AgentConfig.fleet_release_id`` defaulted to
``""`` instead, and ``""`` is falsy but is not ``None`` — so every unpinned
root run took the fleet-snapshot branch of ``load_child_config`` and every
spawn on every instance was refused. These tests cross that gap.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from robothor.engine.models import AgentConfig, SpawnContext
from robothor.engine.spawn_context import make_spawn_context
from robothor.engine.spawn_release import load_child_config


def _session(**overrides):
    run = SimpleNamespace(
        id="run-1",
        tracking_disabled=False,
        correlation_id="corr-1",
        token_budget=0,
        person_id=None,
        **overrides,
    )
    return SimpleNamespace(run=run, identity=None)


def test_root_context_from_an_unpinned_manifest_is_not_release_pinned():
    """No manifest key can set fleet_release_id, so an ordinary agent is unpinned."""
    context = make_spawn_context(AgentConfig(id="main", name="Main", can_spawn_agents=True), _session(), None)

    assert context.fleet_release_id is None


def test_root_context_keeps_a_release_the_snapshot_loader_pinned():
    """A child of a reviewed fleet run still resolves inside that release."""
    config = AgentConfig(id="main", name="Main", can_spawn_agents=True)
    config.fleet_release_id = "a" * 64

    assert make_spawn_context(config, _session(), None).fleet_release_id == "a" * 64


async def test_unpinned_root_spawn_resolves_from_live_manifests(monkeypatch, tmp_path):
    """The end-to-end defect: a root spawn was refused for every agent."""
    loaded = {}

    def fake_load(agent_id, manifest_dir, caller):
        loaded["agent_id"] = agent_id
        return AgentConfig(id=agent_id, name=agent_id), ""

    def refuse(*args, **kwargs):
        raise AssertionError("An unpinned root run must not read a fleet release")

    monkeypatch.setattr("robothor.engine.config.load_agent_config_or_reason", fake_load)
    monkeypatch.setattr("robothor.templates.fleet_store.staged_release_path", refuse)
    context = make_spawn_context(AgentConfig(id="main", name="Main", can_spawn_agents=True), _session(), None)
    engine_config = SimpleNamespace(workspace=tmp_path, manifest_dir=tmp_path / "docs/agents")

    child, reason = await load_child_config("worker", engine_config, context.fleet_release_id)

    assert reason == ""
    assert child is not None
    assert loaded["agent_id"] == "worker"


@pytest.mark.parametrize("empty", ["", None])
async def test_no_falsy_release_reaches_the_snapshot_loader(monkeypatch, tmp_path, empty):
    """`staged_release_path(workspace, "")` is not a release lookup, it is a bug."""

    def refuse(*args, **kwargs):
        raise AssertionError("An empty release id is not a reviewed fleet release")

    monkeypatch.setattr(
        "robothor.engine.config.load_agent_config_or_reason",
        lambda agent_id, manifest_dir, caller: (AgentConfig(id=agent_id, name=agent_id), ""),
    )
    monkeypatch.setattr("robothor.templates.fleet_store.staged_release_path", refuse)
    engine_config = SimpleNamespace(workspace=tmp_path, manifest_dir=tmp_path / "docs/agents")

    child, reason = await load_child_config("worker", engine_config, empty)

    assert reason == ""
    assert child is not None


def test_the_two_release_defaults_cannot_drift_apart():
    """The defect was two dataclasses disagreeing about the same field."""
    fields = {
        cls.__name__: next(f for f in dataclasses.fields(cls) if f.name == "fleet_release_id")
        for cls in (AgentConfig, SpawnContext)
    }

    assert {name: f.default for name, f in fields.items()} == {
        "AgentConfig": None,
        "SpawnContext": None,
    }
    assert {name: str(f.type) for name, f in fields.items()} == {
        "AgentConfig": "str | None",
        "SpawnContext": "str | None",
    }
