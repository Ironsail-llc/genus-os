"""Every shipped agent template validates strictly.

This is the test that makes `ROBOTHOR_MANIFEST_SCHEMA_MODE=enforce` a real
option rather than a flag nobody dares turn on. A fresh install scaffolds its
fleet from `templates/agents/**`; if one of those carries a key the schema has
never heard of, the first thing a new operator sees under `enforce` is their
own agents reported broken.

`strict=True` on purpose: an unrecognised key in a LIVE instance manifest may
be a plugin field or a future option, but in a template that ships from this
repo it is a bug in the template or a gap in the schema. There is no third
possibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from robothor.engine.manifest_schema import errors, validate
from robothor.templates.resolver import TemplateResolver

REPO = Path(__file__).resolve().parents[3]
TEMPLATE_ROOT = REPO / "templates" / "agents"


def _bundles() -> list[Path]:
    return sorted(p.parent for p in TEMPLATE_ROOT.glob("*/*/manifest.template.yaml"))


def _render(bundle: Path) -> dict:
    """Resolve one bundle's manifest the way the installer does.

    The templates are Jinja-ish sources — `primary: {{ model_primary }}` is not
    even parseable YAML — so they have to go through the resolver with the same
    context the installer builds, or this test would validate a different
    document than the one an install produces.
    """
    resolver = TemplateResolver()
    defaults = yaml.safe_load((TEMPLATE_ROOT / "_defaults.yaml").read_text(encoding="utf-8")) or {}
    setup_path = bundle / "setup.yaml"
    setup = yaml.safe_load(setup_path.read_text(encoding="utf-8")) if setup_path.is_file() else {}
    context = resolver.build_context(setup_yaml=setup or {}, defaults_yaml=defaults)
    content = resolver.resolve_file(bundle / "manifest.template.yaml", context, trusted_root=bundle)
    return yaml.safe_load(content)


def test_there_are_templates_to_check():
    """A glob that matches nothing is a green test that checks nothing."""
    assert len(_bundles()) >= 10


@pytest.mark.parametrize("bundle", _bundles(), ids=lambda p: p.name)
def test_template_manifest_is_strict_clean(bundle: Path):
    data = _render(bundle)
    found = errors(validate(data, strict=True))
    assert not found, "\n".join(f"  {i.path}: {i.code} — {i.message}" for i in found)
