"""No agent tool may reach the control plane. Structural, enforced by CI."""

import re

from robothor.engine.tools import schemas


def test_no_tool_exposes_flag_control():
    # schemas.py exposes get_engine_schemas(); scan both the built registry and
    # the module source so a control tool cannot slip in by either route.
    import inspect

    built = " ".join(schemas.get_engine_schemas().keys())
    src = inspect.getsource(schemas)
    assert not re.search(r"set_flag|guardrail_mode|feature_flag|control_flag", built + src), (
        "a control tool in schemas.py would give a prompt-injected agent a path "
        "to disable every guardrail — the write path must be operator-only and "
        "have NO agent-facing tool at all"
    )
