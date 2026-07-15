"""No agent tool may reach the control plane. Structural, enforced by CI."""

import re

from robothor.engine.tools import schemas


def test_no_agent_tool_can_control_guardrails():
    # Inspect the ACTUAL agent-facing tool surface: the built tool registry,
    # not the module's raw source. Scanning source falsely flags legitimate
    # internal imports (e.g. reading a rollout gate) that never register a
    # tool at all. What matters is what an agent can call and what it's told
    # that call does — the registry's tool names and descriptions.
    built = schemas.get_engine_schemas()
    hay = " ".join(
        name
        + " "
        + f.get("function", {}).get("name", "")
        + " "
        + f.get("function", {}).get("description", "")
        for name, f in built.items()
    ).lower()
    assert not re.search(
        r"set_flag|guardrail|feature_flag|control_flag|governed_flag|"
        r"toggle.*(rbac|sandbox|injection)|promote.*control",
        hay,
    ), (
        "an agent-facing tool can reach the guardrail control plane — the "
        "write path must be operator-only and have NO agent-facing tool at all"
    )
