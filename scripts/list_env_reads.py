#!/usr/bin/env python3
"""Discover every ``ROBOTHOR_*``/``GENUS_*`` configuration name the platform reads.

This is the input to ``tests/test_settings_registry.py``: a name that is read
anywhere in the shipped platform must be a declared field (or a declared
deprecated alias) of ``robothor.settings.GenusSettings``. "Undocumented
configuration variable" is the defect this makes impossible.

Two scanners:

* **Python** (``robothor/``, ``crm/bridge/``, ``scripts/*.py``) — an AST walk,
  not a grep, so the name is only counted when it is genuinely *read* as an
  environment variable: ``os.environ["X"]``, ``os.environ.get("X")``,
  ``os.getenv("X")``, bare ``environ.get``/``getenv`` (the ``from os import``
  forms), ``"X" in os.environ``, and the mutating ``setdefault``/``pop``
  accessors.
* **Non-Python** (systemd units and drop-ins, ``*.env.example``, Helm
  templates, dashboard TypeScript) — a regex, because there is no syntax to
  walk. TypeScript is narrowed to ``process.env`` references so a string that
  merely mentions a name in prose is not counted.

Test-only names are excluded: a name read solely by a test file configures the
test, not the product. See ``_is_test_path``.

Usage::

    python scripts/list_env_reads.py             # one name per line
    python scripts/list_env_reads.py --sites      # name + every call site
    python scripts/list_env_reads.py --json       # {name: [site, ...]}
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Only these two prefixes are platform configuration. Third-party names
#: (``OPENAI_API_KEY``, ``REDIS_URL``, ``PATH``) are declared where they are
#: relevant but are not part of the ratchet -- they are not ours to rename.
NAME_RE = re.compile(r"^(?:ROBOTHOR|GENUS)_[A-Z0-9_]+$")

#: Same pattern, unanchored, for scanning non-Python files. The final character
#: must not be an underscore so that a prose wildcard -- ``ROBOTHOR_THERMAL_*``
#: in a unit-file comment -- does not register as a variable named
#: ``ROBOTHOR_THERMAL_``.
NAME_SCAN_RE = re.compile(r"\b(?:ROBOTHOR|GENUS)_[A-Z0-9_]*[A-Z0-9]\b")

#: Attribute accessors on a mapping that behave as an environment read.
_ENV_ACCESSORS = frozenset({"get", "setdefault", "pop"})

#: Python roots scanned by the AST walker. Directories are walked recursively.
PYTHON_ROOTS: tuple[str, ...] = ("robothor", "crm/bridge", "scripts")

#: Non-Python globs scanned by the regex walker.
#:
#: Shell is in here deliberately. The guardrails that watch backups, restores,
#: thermals and the SLOs are shell, not Python, and an operator configuring one
#: has exactly the same problem as an operator configuring the engine. Leaving
#: them out was leaving ~70 names undeclared.
#:
#: The Helm entry is ``**``-recursive and covers ``.tpl``: the chart's env
#: names live in ``templates/_helpers.tpl``, and a
#: ``templates/*.yaml`` glob matched five files carrying none of them --
#: a scan that ran and found nothing, which reads exactly like a scan that
#: found nothing to find.
TEXT_GLOBS: tuple[str, ...] = (
    "infra/systemd/*.service",
    "infra/systemd/*.conf",
    "infra/systemd/*.env.example",
    "infra/*.env.example",
    "infra/**/*.sh",
    "infra/bin/*",
    "scripts/**/*.sh",
    "helm/genus-os/**/*.yaml",
    "helm/genus-os/**/*.tpl",
    "Dockerfile*",
    ".github/workflows/*.yml",
)

#: Dashboard sources: only server-side ``process.env`` reads count.
TS_ROOTS: tuple[str, ...] = ("app/src",)
TS_SUFFIXES: tuple[str, ...] = (".ts", ".tsx")
TS_ENV_RE = re.compile(
    r"process\.env(?:\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\s*[\"'](?P<key>[^\"']+)[\"']\s*\])"
)


def _is_test_path(path: Path) -> bool:
    """True when ``path`` is test scaffolding rather than shipped platform code.

    A name only a test reads configures the test run, not the product, so it
    is out of scope for the declaration ratchet. Covers both layouts in this
    repo: top-level ``tests/`` trees and package-local ``robothor/*/tests/``.
    """
    parts = path.parts
    if "tests" in parts or "test" in parts:
        return True
    name = path.name
    return name.startswith("test_") or name == "conftest.py"


def _is_env_mapping(node: ast.expr) -> bool:
    """True when ``node`` evaluates to the process environment mapping.

    Accepts ``os.environ`` (and any ``<module>.environ``, which in practice is
    only ``os``), plus a bare ``environ`` name from ``from os import environ``.
    """
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_getenv(func: ast.expr) -> bool:
    """True for ``os.getenv`` and a bare ``getenv`` imported from ``os``."""
    if isinstance(func, ast.Attribute):
        return func.attr == "getenv"
    return isinstance(func, ast.Name) and func.id == "getenv"


def _literal_name(node: ast.expr | None) -> str | None:
    """Return ``node``'s string value when it is a platform config name."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and NAME_RE.fullmatch(node.value)
    ):
        return node.value
    return None


def env_names_in_python_source(source: str) -> set[str]:
    """Every platform config name read as an env var in ``source``.

    Raises ``SyntaxError`` if ``source`` does not parse -- callers decide
    whether an unparseable file is fatal.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if _is_getenv(func) or (
                isinstance(func, ast.Attribute)
                and func.attr in _ENV_ACCESSORS
                and _is_env_mapping(func.value)
            ):
                name = _literal_name(node.args[0]) if node.args else None
            else:
                name = None
            if name:
                found.add(name)
        elif isinstance(node, ast.Subscript) and _is_env_mapping(node.value):
            name = _literal_name(node.slice)
            if name:
                found.add(name)
        elif isinstance(node, ast.Compare):
            # `"X" in os.environ` / `"X" not in os.environ`
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_env_mapping(comparator):
                    name = _literal_name(node.left)
                    if name:
                        found.add(name)
    return found


def _python_files(root: Path) -> Iterator[Path]:
    base = root
    if base.is_file():
        if base.suffix == ".py":
            yield base
        return
    for path in sorted(base.rglob("*.py")):
        if _is_test_path(path.relative_to(REPO_ROOT)):
            continue
        yield path


def scan_python(repo_root: Path = REPO_ROOT) -> dict[str, set[str]]:
    """Map each config name to the ``path:line``-free set of files reading it."""
    sites: dict[str, set[str]] = {}
    for rel in PYTHON_ROOTS:
        for path in _python_files(repo_root / rel):
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                names = env_names_in_python_source(source)
            except SyntaxError:
                # A file that does not parse cannot be reasoned about; skip it
                # rather than fail the whole scan (templates land here).
                continue
            for name in names:
                sites.setdefault(name, set()).add(str(path.relative_to(repo_root)))
    return sites


def scan_text(repo_root: Path = REPO_ROOT) -> dict[str, set[str]]:
    """Regex scan of units, env examples and Helm templates."""
    sites: dict[str, set[str]] = {}
    for pattern in TEXT_GLOBS:
        for path in sorted(repo_root.glob(pattern)):
            if _is_test_path(path.relative_to(repo_root)):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in NAME_SCAN_RE.finditer(text):
                sites.setdefault(match.group(0), set()).add(str(path.relative_to(repo_root)))
    return sites


def scan_typescript(repo_root: Path = REPO_ROOT) -> dict[str, set[str]]:
    """Scan dashboard sources for ``process.env`` reads of platform names."""
    sites: dict[str, set[str]] = {}
    for rel in TS_ROOTS:
        root = repo_root / rel
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in TS_SUFFIXES or not path.is_file():
                continue
            if _is_test_path(path.relative_to(repo_root)):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in TS_ENV_RE.finditer(text):
                name = match.group("attr") or match.group("key") or ""
                if NAME_RE.fullmatch(name):
                    sites.setdefault(name, set()).add(str(path.relative_to(repo_root)))
    return sites


def discover(repo_root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Every platform config name the shipped platform reads, with its sites."""
    merged: dict[str, set[str]] = {}
    for scanner in (scan_python, scan_text, scan_typescript):
        for name, paths in scanner(repo_root).items():
            merged.setdefault(name, set()).update(paths)
    return {name: sorted(paths) for name, paths in sorted(merged.items())}


# --------------------------------------------------------------------------
# Ratchet support: how many env-read call sites still live outside the model.
# --------------------------------------------------------------------------


def count_env_read_sites(
    repo_root: Path = REPO_ROOT, exclude: Iterable[str] = ("robothor/settings",)
) -> int:
    """Count Python env-read *call sites* outside the settings package.

    Unlike :func:`discover` this counts occurrences, not distinct names, and
    counts every env read -- third-party names included -- because the goal of
    the ratchet is that readers move behind ``get_settings()``, whatever the
    variable is called.
    """
    excluded = tuple(exclude)
    total = 0
    for rel in PYTHON_ROOTS:
        for path in _python_files(repo_root / rel):
            relative = str(path.relative_to(repo_root))
            if any(relative.startswith(prefix) for prefix in excluded):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if _is_getenv(func) or (
                        isinstance(func, ast.Attribute)
                        and func.attr in _ENV_ACCESSORS
                        and _is_env_mapping(func.value)
                    ):
                        total += 1
                elif isinstance(node, ast.Subscript) and _is_env_mapping(node.value):
                    total += 1
                elif isinstance(node, ast.Compare):
                    # `"X" in os.environ` is a read like any other: a caller
                    # branching on a variable's presence is a caller that has
                    # to move behind get_settings() too. Counting the same
                    # shapes the discovery counts keeps the two honest.
                    for op, comparator in zip(node.ops, node.comparators, strict=True):
                        if isinstance(op, (ast.In, ast.NotIn)) and _is_env_mapping(comparator):
                            total += 1
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit {name: [site, ...]} as JSON")
    parser.add_argument("--sites", action="store_true", help="print each name with its files")
    parser.add_argument(
        "--count-sites",
        action="store_true",
        help="print the number of Python env-read call sites outside robothor/settings",
    )
    args = parser.parse_args(argv)

    if args.count_sites:
        print(count_env_read_sites())
        return 0

    names = discover()
    if args.json:
        json.dump(names, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    elif args.sites:
        for name, paths in names.items():
            print(name)
            for path in paths:
                print(f"    {path}")
    else:
        for name in names:
            print(name)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
