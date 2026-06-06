"""Manifest tool resolvability (Wave-1 hardening, PR-2).

A manifest that declares a tool with no registered schema/adapter route used to
have that tool *silently* dropped at ``ToolRegistry._get_filtered_names`` — which
is how dead declarations (e.g. an instance ``main.yaml`` listing memory_vault_*/
intent_*/recall_node tools that were never implemented) went unnoticed. The
platform fix is to make the drop VISIBLE: log a warning naming the unresolved
tools so any instance can see manifest/implementation drift. Behavior is
otherwise unchanged (the tools are still dropped).
"""

from __future__ import annotations

import dataclasses
import logging


def _registry():
    from robothor.engine.tools.registry import ToolRegistry

    return ToolRegistry()


def test_unresolved_tool_is_dropped_and_warned(sample_agent_config, caplog):
    reg = _registry()
    real = sorted(reg._schemas)[0]  # a genuinely-registered tool
    cfg = dataclasses.replace(sample_agent_config, tools_allowed=[real, "totally_bogus_tool_xyz"])
    with caplog.at_level(logging.WARNING, logger="robothor.engine.tools.registry"):
        names = reg.get_tool_names(cfg)

    assert real in names
    assert "totally_bogus_tool_xyz" not in names  # behavior unchanged: still dropped
    assert any("totally_bogus_tool_xyz" in r.getMessage() for r in caplog.records)


def test_no_warning_when_all_tools_resolve(sample_agent_config, caplog):
    reg = _registry()
    real = sorted(reg._schemas)[:2]
    cfg = dataclasses.replace(sample_agent_config, tools_allowed=real)
    with caplog.at_level(logging.WARNING, logger="robothor.engine.tools.registry"):
        reg.get_tool_names(cfg)
    assert not any("no registered schema" in r.getMessage() for r in caplog.records)


def test_warns_once_per_agent(sample_agent_config, caplog):
    """_get_filtered_names runs on every build/list call — don't spam the log."""
    reg = _registry()
    real = sorted(reg._schemas)[0]
    cfg = dataclasses.replace(sample_agent_config, tools_allowed=[real, "bogus_xyz"])
    with caplog.at_level(logging.WARNING, logger="robothor.engine.tools.registry"):
        reg.get_tool_names(cfg)
        reg.build_for_agent(cfg)
        reg.get_tool_names(cfg)
    hits = [r for r in caplog.records if "bogus_xyz" in r.getMessage()]
    assert len(hits) == 1
