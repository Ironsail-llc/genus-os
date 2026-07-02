"""Inter-agent messaging + teams activation (Wave-1 hardening, PR-13).

The handlers existed but had no schemas (so agents never saw the tools) and the
daemon never called init_messenger/init_team_manager (so the handlers errored
"not initialized"). This wires the schemas and asserts the daemon inits them.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

from robothor.engine.tools.schemas import get_engine_schemas

_ROOT = Path(__file__).resolve().parents[3]

MESSAGING_TOOLS = [
    "send_agent_message",
    "receive_agent_messages",
    "create_team",
    "team_scratchpad_write",
    "team_scratchpad_read",
]


def test_schemas_present():
    schemas = get_engine_schemas()
    for name in MESSAGING_TOOLS:
        assert name in schemas
        assert schemas[name]["function"]["name"] == name


def test_handlers_back_every_schema():
    from robothor.engine.tools.handlers.messaging import HANDLERS

    for name in MESSAGING_TOOLS:
        assert name in HANDLERS


def test_registry_exposes_them(sample_agent_config):
    from robothor.engine.tools.registry import ToolRegistry

    reg = ToolRegistry()
    cfg = dataclasses.replace(sample_agent_config, tools_allowed=MESSAGING_TOOLS)
    names = reg.get_tool_names(cfg)
    assert set(MESSAGING_TOOLS).issubset(set(names))


def test_daemon_initializes_messaging_and_teams():
    from robothor.engine import daemon

    src = inspect.getsource(daemon)
    assert "init_messenger()" in src
    assert "init_team_manager()" in src
