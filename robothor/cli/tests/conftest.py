"""Workspace containment for the CLI tests.

This is the package the incident came from: ``test_setup_link_cli.py`` called
``run_init`` with an args object that had no ``dry_run`` attribute, so a real
init ran and its agents step resolved ``ROBOTHOR_WORKSPACE`` — a live instance.
That test pinned ``HOME`` and not ``ROBOTHOR_WORKSPACE``, which was enough to
look careful and not enough to be safe.

The fixture and the reasoning live in
``tests/conftest_workspace_containment.py``; this file is the wiring.
"""

from __future__ import annotations

from tests.conftest_workspace_containment import (  # noqa: F401 - pytest collects these
    assert_contained,
    contained_workspace,
    env_workspace,
)
