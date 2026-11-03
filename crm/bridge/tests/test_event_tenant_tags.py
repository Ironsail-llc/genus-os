"""Every event the bridge publishes names its tenant in the envelope.

One engine runs per tenant and every engine reads every stream; an engine acts
only on events whose envelope ``tenant_id`` is its own. The bridge serves all
tenants from one process and never enters a ``tenant_scope``, so the bus's
fallback (the process default tenant) would file every child tenant's
``task.created`` under the root. Each call must pass ``tenant_id=`` itself --
putting it in the payload is not enough, the engines route on the envelope.
"""

from __future__ import annotations

import ast
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent


def _publish_calls() -> list[tuple[str, int, ast.Call]]:
    found = []
    for path in sorted(BRIDGE_DIR.rglob("*.py")):
        if "tests" in path.relative_to(BRIDGE_DIR).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"publish", "publish_event"}:
                found.append((str(path.relative_to(BRIDGE_DIR)), node.lineno, node))
    return found


def test_the_scan_finds_the_bridge_publishers():
    assert len(_publish_calls()) >= 5, "the AST scan no longer sees the bridge's publish calls"


def test_every_bridge_publish_passes_tenant_id():
    missing = [
        f"{path}:{line}"
        for path, line, call in _publish_calls()
        if not any(kw.arg == "tenant_id" for kw in call.keywords)
    ]
    assert not missing, "publish() without tenant_id= at:\n  " + "\n  ".join(missing)
