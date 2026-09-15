"""What the exec scrub is, and what only a sandbox can be.

Review finding C2, which is the one finding that cannot be fully fixed in this
layer — so the honest response is three real mitigations and a docstring that
stops overclaiming.

The claim that was wrong: ``exec_env``'s own docstring said "the fix has to be
that the credential is not there", and the configuration doc said a sub-agent
sees "Nothing". Removing a variable from a child's environment does not remove
it from the ENGINE's, and ``/proc/<pid>/environ`` is readable by any process of
the same uid. ``subprocess.run(shell=True)`` makes the engine the parent of
``sh``, so ``$PPID`` in any agent command IS the engine, whose initial
environment block on a systemd instance is the whole decrypted secrets file.
The probe read a fake out of a helper process's ``/proc`` under ``enforce``,
with no grants, as a worker agent.

What this file pins:

1. ``PR_SET_DUMPABLE=0`` — makes the engine's own ``/proc/<pid>/environ``,
   ``maps`` and ``fd`` root-only, so a same-uid child cannot read them. This is
   the mitigation that actually closes the probe, and it is a real kernel
   boundary rather than a denylist.
2. ``secret_paths`` refuses the shapes that read an environment out of procfs
   or a shell builtin. Defence in depth, and a denylist, and it is labelled as
   one: it raises the cost, it does not close the hole.
3. The scrub is described as what it is — removal of AMBIENT INHERITANCE — in
   the docstring and the docs. The boundary is the sandbox; the remedy is the
   SOPS shrink, after which procfs leaks only bootstrap credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from robothor.engine.secret_paths import exec_reads_secret

FAKE = "ghp_FAKE0000CANARYaaaaaaaaaaaaaaaaaaa"


# ── 1. the engine is not readable through procfs ─────────────────────────────


def test_the_engine_hardens_its_own_procfs_entry():
    """``harden_process`` is what makes the probe fail.

    Read back through ``PR_GET_DUMPABLE`` rather than ``/proc/self/status``:
    ``Dumpable:`` is not a field every kernel emits, and a test that looks for
    a line that is not there would pass by finding nothing.
    """
    root = os.environ.get("PYTHONPATH", ".")
    script = (
        "import ctypes, ctypes.util, sys;"
        f"sys.path.insert(0, {root!r});"
        "from robothor.engine.process_hardening import harden_process;"
        "took = harden_process();"
        "libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6');"
        "print(took, libc.prctl(3, 0, 0, 0, 0))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    took, dumpable = proc.stdout.split()
    assert took == "True", "hardening did not apply on a platform that supports it"
    assert dumpable == "0", (
        f"PR_GET_DUMPABLE is {dumpable}, so any same-uid process can still read "
        "this process's environment out of /proc"
    )


def test_hardening_never_raises_on_a_platform_without_it():
    """A hardening step that can crash the engine is worse than none: this runs
    at startup, before anything is serving."""
    from robothor.engine.process_hardening import harden_process

    assert harden_process() in {True, False}


def test_a_same_uid_reader_cannot_take_the_environment_of_a_hardened_process():
    """The probe, run rather than reasoned about."""
    if not sys.platform.startswith("linux"):
        pytest.skip("procfs is a Linux interface")

    root = os.environ.get("PYTHONPATH", ".")
    helper = (
        "import sys;"
        f"sys.path.insert(0, {root!r});"
        "from robothor.engine.process_hardening import harden_process;"
        "harden_process();"
        "print('ready', flush=True);"
        "sys.stdin.read()"
    )
    child = subprocess.Popen(  # noqa: S603 - a fixture process, argv is a literal
        [sys.executable, "-c", helper],
        env={**os.environ, "CANARY_TOKEN": FAKE},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        try:
            with open(f"/proc/{child.pid}/environ", "rb") as handle:  # noqa: PTH123
                leaked = FAKE.encode() in handle.read()
        except PermissionError:
            leaked = False
        assert not leaked, (
            "a same-uid process read the canary out of a hardened process's "
            "environment — PR_SET_DUMPABLE is not taking effect"
        )
    finally:
        child.kill()
        child.wait(timeout=10)


# ── 2. the denylist, labelled as a denylist ──────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/self/environ",
        "cat /proc/1234/environ",
        "tr '\\0' '\\n' < /proc/self/environ",
        "strings /proc/$PPID/environ",
        "grep -a FAKE /proc/$PPID/environ",
        "cat /proc/$PPID/cmdline",
        "set",
        "declare -p",
        "typeset -p",
    ],
)
def test_the_shapes_that_read_an_environment_are_refused(command):
    assert exec_reads_secret(command) is not None, f"{command!r} was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/loadavg",
        "cat /proc/meminfo",
        "ls /proc",
        "set -e && python3 -m pytest",
        "set -euo pipefail",
        "declare -i count=3",
    ],
)
def test_ordinary_proc_and_shell_use_is_not_refused(command):
    """A denylist that eats ``set -e`` makes every agent script fail, which is
    how a control gets turned off."""
    assert exec_reads_secret(command) is None, f"{command!r} was refused"


# ── 3. the claim ─────────────────────────────────────────────────────────────


def test_the_module_does_not_claim_to_be_a_boundary():
    """The docstring said "the fix has to be that the credential is not there".

    It is still there — in the ENGINE. Saying otherwise is how an operator
    comes to believe a sub-agent is contained when it is not, and a wrong
    mental model is the thing that gets acted on.
    """
    import robothor.engine.exec_env as exec_env

    doc = (exec_env.__doc__ or "").lower()
    assert "ambient" in doc, "the docstring does not say what the scrub removes"
    assert "not a boundary" in doc or "is not a security boundary" in doc, (
        "the docstring does not say what the scrub is NOT"
    )
    assert "/proc" in doc, "the docstring does not name the way around it"


def test_the_configuration_doc_does_not_promise_nothing():
    """The doc said a sub-agent sees "Nothing, unless its own manifest says
    so". Under ``enforce`` and before the SOPS shrink, it can still reach the
    engine's own environment; the doc has to say so."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[3] / "docs" / "configuration.md"
    text = doc.read_text(encoding="utf-8")
    assert "/proc" in text, "the residual procfs exposure is not documented"
