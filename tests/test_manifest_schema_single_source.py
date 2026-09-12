"""One schema, two files, zero drift.

``robothor/engine/schema/agent_manifest.yaml`` is CANONICAL: it is what
``robothor/engine/manifest_schema.py`` reads at import time and what ships in
the wheel (``packages = ["robothor"]``). ``docs/agents/schema.yaml`` is the
documentation mirror that ``scripts/validate_agents.py`` and the agent-
building docs point at.

Two copies of anything in this repo have historically drifted — the v2 key
list, the guardrail list, the alert-name list. The difference here is that
drift would be invisible: the docs would describe a field the engine rejects,
or accept one it ignores. Byte-equality is the cheapest possible guard, so it
is the one used.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "robothor" / "engine" / "schema" / "agent_manifest.yaml"
MIRROR = REPO / "docs" / "agents" / "schema.yaml"


def test_both_copies_exist():
    assert CANONICAL.is_file(), f"the canonical schema is missing: {CANONICAL}"
    assert MIRROR.is_file(), f"the documentation mirror is missing: {MIRROR}"


def test_the_two_copies_are_byte_equal():
    assert CANONICAL.read_bytes() == MIRROR.read_bytes(), (
        "docs/agents/schema.yaml and robothor/engine/schema/agent_manifest.yaml "
        "have diverged. The package copy is canonical — copy it over the mirror "
        "(cp robothor/engine/schema/agent_manifest.yaml docs/agents/schema.yaml)."
    )


def test_the_canonical_copy_names_itself_canonical():
    """A reader who opens the mirror first must be told which one wins."""
    assert "CANONICAL COPY: robothor/engine/schema/agent_manifest.yaml" in MIRROR.read_text(
        encoding="utf-8"
    )


class TestItShipsInTheWheel:
    """A validator whose schema is missing from the wheel validates nothing.

    hatchling's wheel target takes `packages = ["robothor"]` and ships every
    file under that tree that git does not ignore — data files included, no
    package-data stanza needed. Two things have to hold for that to reach the
    schema, and each has failed for some other file in this repo before: the
    file must be INSIDE the package tree (the repo-root `templates/` dir needs
    an explicit force-include precisely because it is not), and it must be
    git-tracked (`.gitignore` swallowed docs/agents/*.yaml wholesale until a
    `!` negation rescued the schema).

    Building a wheel here would be the direct proof and is too slow to run on
    every commit, so these are the two config preconditions plus the one thing
    a wheel actually has to satisfy at runtime: the file is reachable through
    the package resource API, not through a repo-relative path.
    """

    def test_the_schema_is_inside_the_packaged_tree(self):
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        packages = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        assert "robothor" in packages
        assert CANONICAL.relative_to(REPO).parts[0] == "robothor"

    def test_the_schema_is_git_tracked(self):
        """Untracked means VCS-ignored means absent from the wheel — silently.

        Skipped, not failed, outside a git checkout: this suite also runs from
        an unpacked sdist, where there is no index to ask and the question is
        meaningless rather than answered "no".
        """
        inside = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            pytest.skip("not a git checkout — nothing to ask about tracking")
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "--error-unmatch", str(CANONICAL)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 0, (
            "robothor/engine/schema/agent_manifest.yaml is not tracked by git, "
            "so hatchling will omit it from the wheel and every installed "
            "instance will run with structural validation silently off"
        )

    def test_it_is_readable_through_the_package_resource_api(self):
        """The question a wheel install actually asks.

        `importlib.resources` resolves through the installed package, not the
        repo layout, so this fails for the same reason a wheel would: the data
        file is not in the package. It also proves the directory is importable
        as a package resource root.
        """
        from importlib.resources import files

        resource = files("robothor.engine.schema").joinpath("agent_manifest.yaml")
        assert resource.is_file()
        assert resource.read_bytes() == CANONICAL.read_bytes()

    def test_the_installed_module_finds_it_by_package_path(self):
        """The loader resolves the schema relative to its own module file, so
        a wheel install and a source checkout take the identical path."""
        from robothor.engine import manifest_schema

        assert manifest_schema.SCHEMA_PATH.name == "agent_manifest.yaml"
        assert manifest_schema.SCHEMA_PATH.parent.parent.name == "engine"
        assert manifest_schema.required_keys(), "the schema loaded but yielded no required keys"
