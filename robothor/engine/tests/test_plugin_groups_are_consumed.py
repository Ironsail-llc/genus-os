"""Every declared extension point must have a production consumer.

This is the regression guard for the defect class that dominated 2026-08-26.
#411 declared four entry-point groups. THREE of them did nothing:

  genus.tools       loaded, never advertised to the model     fixed #421
  genus.schemas     nothing wrote them into ToolRegistry      fixed #421
  genus.guardrails  no consumer outside the loader            fixed #424
  genus.hooks       registered nowhere                        fixed #425

Each shipped with passing tests, because those tests asserted against the
loader's own return dataclass — "load_plugins() returns my tool" — and never
against the thing that had to consume it. The suites certified the gaps.

So this test does not check that the loader parses a group. It checks that
some production module reads the corresponding attribute of the PluginSet.
A group nobody reads is a promise in docs/PLUGINS.md that the engine does not
keep.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

from robothor.plugins.loader import _GROUPS, PluginSet

#: Where a consumer may live. The loader itself and tests do not count —
#: consuming your own output is what made three groups look wired.
_PACKAGE = Path(__file__).resolve().parents[2]
_EXCLUDED = ("plugins/loader.py", "plugins/__init__.py")


def _production_files() -> list[Path]:
    return [
        p
        for p in _PACKAGE.rglob("*.py")
        if "tests" not in p.parts
        and not p.name.startswith("test_")
        and not any(str(p).endswith(x) for x in _EXCLUDED)
    ]


@functools.cache
def _bound_trees() -> tuple[tuple[Path, ast.AST, frozenset[str]], ...]:
    """Every production file that binds a name to load_plugins(...), parsed ONCE.

    The first version re-parsed the whole package for every declared group;
    on a slow CI runner that crossed pytest-timeout's 30s and failed main.
    """
    out: list[tuple[Path, ast.AST, frozenset[str]]] = []
    for path in _production_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "load_plugins":
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bound.add(target.id)
        if bound:
            out.append((path, tree, frozenset(bound)))
    return tuple(out)


def _readers_of(attribute: str) -> list[str]:
    """Production files that read `<result of load_plugins()>.<attribute>`.

    AST, not substring. The first version of this test grepped for
    ".tools" and matched `from robothor.engine.tools import ...` in unrelated
    modules, so it passed with the consumer deleted. Six guards were vacuous
    this way in one day; a text match on an attribute name is not evidence
    that anything reads it.
    """
    hits: list[str] = []
    for path, tree, bound in _bound_trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == attribute
                and isinstance(node.value, ast.Name)
                and node.value.id in bound
            ):
                hits.append(str(path.relative_to(_PACKAGE.parent)))
                break
    return hits


def test_the_scan_root_is_real():
    """A wrong root makes every assertion below vacuous — that exact mistake
    shipped in another guard earlier the same day."""
    assert _PACKAGE.name == "robothor" and (_PACKAGE / "plugins").is_dir()
    assert len(_production_files()) > 100


def test_every_declared_group_has_a_production_consumer():
    unconsumed = {}
    for group in _GROUPS:
        attribute = group.split(".", 1)[1]  # genus.tools -> tools
        assert hasattr(PluginSet(), attribute), f"PluginSet has no {attribute!r}"
        readers = _readers_of(attribute)
        if not readers:
            unconsumed[group] = attribute

    assert not unconsumed, (
        "declared entry-point group(s) with no production consumer: "
        f"{unconsumed} — the loader parses them and nothing reads them, so "
        "docs/PLUGINS.md promises behaviour the engine does not have"
    )


def test_each_group_names_a_real_pluginset_field():
    """A typo'd group key would silently contribute nothing."""
    fields = set(PluginSet().__dict__)
    for group in _GROUPS:
        attribute = group.split(".", 1)[1]
        assert attribute in fields, f"{group!r} maps to {attribute!r}, not a PluginSet field"
