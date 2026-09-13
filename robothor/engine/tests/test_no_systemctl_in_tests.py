"""No engine test may spawn ``systemctl``.

Measured on this PR's first revision, with a logging shim first on ``PATH``:
the engine suite spawned ``systemctl`` ten times -- eight pre-existing
``ollama.service`` probes and two new ``ActiveEnterTimestamp`` reads from the
warmup host-state section. The count *understated* it, because the module-global
60s cache in ``host_state`` suppressed every later test that warmed up ``main``
inside the window, and that same cache meant a section rendered in one test file
could be served to another.

A test suite that shells out to the host's service manager is a suite whose
results depend on the host. It is also how a "no real systemctl in tests"
guarantee stays true by accident until the day it isn't.

``conftest.py``'s autouse ``no_systemctl_in_engine_tests`` makes it structural.
These tests assert the guard exists and actually bites, so removing or
weakening it turns the suite red rather than quietly restoring the spawns.
"""

from __future__ import annotations

import subprocess

import pytest

from robothor.engine import host_state


def test_the_host_state_probe_is_stubbed() -> None:
    """The stub replaces the real probe, not merely shadows it."""
    assert host_state._systemctl_active_enter() is None
    assert getattr(host_state._systemctl_active_enter, "_is_test_stub", False) is True


def test_spawning_systemctl_raises() -> None:
    with pytest.raises(AssertionError, match="systemctl"):
        subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestamp"], check=False)


def test_spawning_systemctl_by_absolute_path_raises() -> None:
    """A guard keyed on the exact string ``systemctl`` is one ``/usr/bin/``
    away from inert."""
    with pytest.raises(AssertionError, match="systemctl"):
        subprocess.run(["/usr/bin/systemctl", "is-active", "anything"], check=False)


def test_other_subprocesses_still_run() -> None:
    """The guard is about one binary, not about banning subprocesses -- a
    blanket ban would have been reverted the first time a git hook needed one."""
    done = subprocess.run(["echo", "ok"], capture_output=True, text=True, check=False)
    assert done.stdout.strip() == "ok"


def test_the_module_cache_is_cleared_between_tests() -> None:
    """A section rendered in one test must not be served to the next."""
    assert host_state._CACHE == {}
