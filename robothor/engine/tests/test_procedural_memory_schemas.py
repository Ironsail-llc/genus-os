"""Procedural-memory tools become callable once schemas exist (Wave-1, PR-5).

The handlers (record_procedure / find_procedure / report_procedure_outcome /
leave_breadcrumb) existed in handlers/memory.py and dispatch already routed
them, but they had no schema, so ToolRegistry filtered them out of every agent.
Adding the engine schemas makes them resolvable.
"""

from __future__ import annotations

import dataclasses

from robothor.engine.tools.schemas import get_engine_schemas

PROCEDURAL_TOOLS = [
    "record_procedure",
    "find_procedure",
    "report_procedure_outcome",
    "leave_breadcrumb",
]


def test_schemas_present_and_well_formed():
    schemas = get_engine_schemas()
    for name in PROCEDURAL_TOOLS:
        assert name in schemas, f"{name} schema missing"
        fn = schemas[name]["function"]
        assert fn["name"] == name
        assert "properties" in fn["parameters"]


def test_required_fields():
    schemas = get_engine_schemas()
    assert schemas["record_procedure"]["function"]["parameters"]["required"] == ["name", "steps"]
    assert schemas["find_procedure"]["function"]["parameters"]["required"] == ["task"]
    assert schemas["report_procedure_outcome"]["function"]["parameters"]["required"] == [
        "procedure_id",
        "success",
    ]
    assert schemas["leave_breadcrumb"]["function"]["parameters"]["required"] == ["content"]


def test_registry_now_exposes_them(sample_agent_config):
    from robothor.engine.tools.registry import ToolRegistry

    reg = ToolRegistry()
    cfg = dataclasses.replace(sample_agent_config, tools_allowed=PROCEDURAL_TOOLS)
    names = reg.get_tool_names(cfg)
    for name in PROCEDURAL_TOOLS:
        assert name in names


def test_handlers_exist_for_each_schema():
    """Every new schema must have a dispatch handler (no schema-without-handler)."""
    from robothor.engine.tools.dispatch import _collect_handlers

    handlers = _collect_handlers()
    for name in PROCEDURAL_TOOLS:
        assert name in handlers
