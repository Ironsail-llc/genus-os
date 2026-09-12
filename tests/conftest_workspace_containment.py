"""Structural containment: no test may reach a real workspace.

Imported by the ``conftest.py`` of every test package that can reach a helper
resolving its own paths. One copy, because two copies of a guard drift and the
drift is invisible until the guard is the only thing standing between a test
suite and an operator's instance.

The incident: a test called ``genus init`` with an args object whose ``dry_run``
attribute did not exist, so it ran a REAL init, whose agents step resolved
``ROBOTHOR_WORKSPACE`` — the developer's live instance — and overwrote ten agent
manifests with their template versions. The repo root's ``conftest.py`` has
guarded the DATABASE half of this for a long time (``ROBOTHOR_DB_NAME`` is
pinned and ``assert_test_database`` hard-fails inside pytest). Nothing guarded
the filesystem half.

Two mechanisms, because either alone rots:

* ``HOME``, ``ROBOTHOR_WORKSPACE`` and ``ROBOTHOR_OWNER_CONFIG`` point at a
  per-test temporary directory, so a helper that resolves its own workspace
  resolves a throwaway one.
* That directory is seeded with a sentinel agent manifest and checked
  byte-for-byte after every test. A redirect nobody verifies is a comment.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

#: What a test that escapes its workspace would overwrite. Shaped like the file
#: the incident destroyed -- a manifest in ``docs/agents/`` -- so the guard
#: fails on the exact write that happened rather than on a generic marker.
SENTINEL_RELATIVE = Path("docs/agents/main.yaml")
SENTINEL_BODY = (
    "# Sentinel for tests/conftest_workspace_containment.py.\n"
    "# A test wrote here, which means it resolved a workspace from the\n"
    "# environment instead of taking one it was given. On a developer's box\n"
    "# that environment is a live instance.\n"
    "id: main\n"
    "name: Main\n"
)

#: Variables a helper might resolve a writable location from.
_PINNED = ("HOME", "ROBOTHOR_WORKSPACE", "ROBOTHOR_OWNER_CONFIG")


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@pytest.fixture(autouse=True)
def contained_workspace(tmp_path_factory, monkeypatch):
    """Point every environment-resolved path at a throwaway, and prove it held.

    Autouse and unconditional. A test that genuinely needs a different
    workspace passes one explicitly, which is the behaviour being enforced.
    """
    root = tmp_path_factory.mktemp("contained-workspace")
    sentinel = root / SENTINEL_RELATIVE
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(SENTINEL_BODY, encoding="utf-8")
    before = _digest(sentinel)

    for name in _PINNED:
        monkeypatch.setenv(name, str(root / "owner.yaml" if "OWNER" in name else root))

    # The settings model caches; a stale cache would hand a step the real
    # instance's paths even with the variables pinned.
    from robothor.settings import reset_settings

    reset_settings()
    try:
        yield root
    finally:
        reset_settings()
        after = _digest(sentinel)
        assert after == before, (
            f"a test wrote to the workspace named by ROBOTHOR_WORKSPACE ({root}). "
            "Every write must go to a path the caller was given -- see "
            "robothor/init/tests/test_workspace_containment.py."
        )


@pytest.fixture
def env_workspace(contained_workspace):
    """The contained workspace, for a test that wants to assert on it."""
    return contained_workspace


def assert_contained() -> Path:
    """The workspace the environment currently resolves to, proven temporary.

    Callable from a test that wants to state the guard is on rather than assume
    it: the assertion is what stops this file becoming decoration.
    """
    resolved = Path(os.environ["ROBOTHOR_WORKSPACE"]).resolve()
    assert "contained-workspace" in str(resolved), (
        f"ROBOTHOR_WORKSPACE is {resolved}, which is not a pytest temporary "
        "directory -- the containment fixture is not active in this package"
    )
    return resolved
