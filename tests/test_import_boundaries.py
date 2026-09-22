"""Enforce the CRM ↔ engine/memory layering.

The CRM data layer (``robothor.crm``) is a bottom layer: the engine and the
bridge both depend on it, so it must not depend on them. The single sanctioned
upward seam is ``robothor.crm.hooks`` (it binds to engine/memory lazily, at call
time, so a plain ``import robothor.crm.dal`` stays clean).

Two guards:
- runtime: importing the DAL must not pull the engine/memory into ``sys.modules``.
- static: no module in ``robothor/crm/`` (except ``hooks.py``) may reference
  ``robothor.engine`` / ``robothor.memory`` at all.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_importing_crm_dal_does_not_import_engine_or_memory() -> None:
    code = (
        "import importlib, sys\n"
        "importlib.import_module('robothor.crm.dal')\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'robothor.engine' or m.startswith('robothor.engine.')\n"
        "    or m == 'robothor.memory' or m.startswith('robothor.memory.')\n"
        ")\n"
        "assert not leaked, 'CRM layer leaked engine/memory imports: ' + repr(leaked)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)


def test_no_engine_or_memory_imports_in_crm_layer() -> None:
    crm_dir = _REPO_ROOT / "robothor" / "crm"
    pattern = re.compile(r"^\s*(?:from|import)\s+robothor\.(engine|memory)\b")
    offenders: list[str] = []
    for py in crm_dir.rglob("*.py"):
        if py.name == "hooks.py":  # the one sanctioned (lazy) upward seam
            continue
        if "tests" in py.parts:
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if pattern.match(line):
                offenders.append(f"{py.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "engine/memory imports found in the CRM layer:\n" + "\n".join(offenders)
