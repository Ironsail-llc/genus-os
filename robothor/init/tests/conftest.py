"""Workspace containment for the wizard's own tests.

These tests drive the steps that write to a workspace, so they are the ones with
the most to gain from being unable to reach a real one. The fixture and the
reasoning live in ``tests/conftest_workspace_containment.py``; this file is the
wiring.
"""

from __future__ import annotations

from tests.conftest_workspace_containment import (  # noqa: F401 - pytest collects these
    assert_contained,
    contained_workspace,
    env_workspace,
)
