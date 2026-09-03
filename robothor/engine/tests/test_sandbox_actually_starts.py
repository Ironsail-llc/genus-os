"""The sandbox has never held a real agent. Prove it can hold one at all.

As of 2026-08-27: 1,744 exec calls in 7 days, none containerised;
`sandbox_default` has 8 observe events ever; exactly one container has ever
existed (2026-07-14, exited 0) and nothing since. A containment layer nobody
has started is indistinguishable from a containment layer that cannot start,
which is the distinction this file exists to make.

It can. Verified by starting a real podman container through `Sandbox.start()`
and listing it: isolation flags applied, container live, cleaned up after.

The runner comment claiming "the engine user is not in the docker group, so
start() cannot succeed at all" predates ROBOTHOR_SANDBOX_BINARY=podman, which
is rootless and needs no group membership.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest


def _binary() -> str | None:
    from robothor.engine.sandbox import sandbox_binary

    b = sandbox_binary()
    return b if b and shutil.which(b) else None


class TestIsolationFlags:
    """The flags ARE the security value of this class; they were once absent
    entirely (no mount, no --network, no --user, no --cap-drop)."""

    def _argv(self) -> list[str]:
        from robothor.engine.sandbox import Sandbox, SandboxMode

        sb = Sandbox(mode=SandboxMode.DOCKER, run_id=uuid.uuid4().hex, workspace="/tmp")
        return sb._run_argv(workspace=sb.workspace)

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("--network", "none"),
            ("--memory", "512m"),
        ],
    )
    def test_flag_carries_its_value(self, flag, value):
        argv = self._argv()
        assert flag in argv, f"{flag} absent — the container would not be isolated"
        assert argv[argv.index(flag) + 1] == value

    def test_user_is_the_invoking_account_and_never_root(self):
        """The uid is whoever ran the engine, which is NOT a constant: this
        asserted 1000:1000 and so passed only on a workstation whose first
        user is the operator. CI runs as 1001 and the gate went red for a
        reason that had nothing to do with isolation.

        The property worth holding is the one the flag exists for — files come
        back owned by the caller, and the container is never root.
        """
        argv = self._argv()
        assert "--user" in argv, "--user absent — the container would run as root"
        assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert argv[argv.index("--user") + 1] != "0:0"

    @pytest.mark.parametrize(
        "token",
        ["--read-only", "--cap-drop=ALL", "--pids-limit=256"],
    )
    def test_standalone_flag_present(self, token):
        assert token in self._argv(), f"{token} absent"

    def test_no_new_privileges_is_set(self):
        argv = self._argv()
        assert any("no-new-privileges" in a for a in argv)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_real_container_actually_starts():
    """The whole point. Skipped where no runtime exists; never mocked --
    a mocked container runtime proves nothing about containment."""
    binary = _binary()
    if not binary:
        pytest.skip("no container runtime available")

    from robothor.engine.sandbox import Sandbox, SandboxMode

    run_id = uuid.uuid4().hex
    name = f"sandbox-{run_id[:12]}"
    sb = Sandbox(mode=SandboxMode.DOCKER, run_id=run_id, workspace="/tmp")
    try:
        await sb.start()
        listed = subprocess.run(
            [binary, "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        assert name in listed, f"start() returned but no container is running: {listed!r}"
    except FileNotFoundError:  # pragma: no cover
        pytest.skip("container runtime disappeared mid-test")
    finally:
        subprocess.run([binary, "rm", "-f", name], capture_output=True, timeout=30)
