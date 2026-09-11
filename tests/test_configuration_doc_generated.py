"""``docs/reference/configuration.md`` is generated, not written.

The committed file must equal what ``scripts/gen_configuration_doc.py`` renders
from the settings registry, so a field added or re-described without
regenerating the reference fails here rather than shipping a doc that disagrees
with the code.

Regenerate with::

    python scripts/gen_configuration_doc.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen_configuration_doc.py"
REFERENCE = REPO_ROOT / "docs" / "reference" / "configuration.md"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_configuration_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reference_is_committed() -> None:
    assert REFERENCE.exists(), f"{REFERENCE} is missing -- run {GENERATOR.name}"


def test_reference_equals_generator_output() -> None:
    rendered = _generator().render()
    assert REFERENCE.read_text(encoding="utf-8") == rendered, (
        "docs/reference/configuration.md is stale -- run `python scripts/gen_configuration_doc.py`"
    )


def test_reference_names_no_secret_values() -> None:
    """A generated table must never print a credential."""
    from robothor.settings.registry import field_index

    text = REFERENCE.read_text(encoding="utf-8")
    for env_name, info in field_index().items():
        if info["secret"]:
            assert env_name in text, f"{env_name} is missing from the reference"
            assert info["default"] in ("", None)
