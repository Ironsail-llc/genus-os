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


class TestStartRetriesOnce:
    """A boot race must not hard-fail a run that declared `sandbox: docker`.

    2026-09-03, twelve minutes after a reboot and never since:

        newuidmap: write to uid_map failed: Operation not permitted

    Rootless podman needs the setuid `newuidmap` helper and the user's
    subuid/subgid ranges; early in boot that plumbing is not always ready. A
    single `subprocess.run` was the whole of `start()`, so one transient
    refusal took the run with it.

    One bounded retry — not a loop. A runtime that is genuinely misconfigured
    must still fail, and fail with its own error, because "cannot start a
    container" is a security-relevant fact that may not be retried into
    silence. Exactly two invocations: the fake binary counts them, so a retry
    that silently became three would fail this.
    """

    @staticmethod
    def _fake_binary(tmp_path, fail_times: int):
        """A runtime that refuses `fail_times` times, then succeeds.

        Counts START attempts only. The retry also issues a `rm -f` to free
        the name it left behind (see TestRetryCleansUpTheNameItLeft), and
        counting that as an attempt would make "exactly two" mean nothing.
        """
        counter = tmp_path / "invocations"
        script = tmp_path / "fake-podman"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "rm" ]; then exit 0; fi\n'
            f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
            "n=$((n+1))\n"
            f'echo "$n" > "{counter}"\n'
            f'if [ "$n" -le {fail_times} ]; then\n'
            '  echo "newuidmap: write to uid_map failed: Operation not permitted" >&2\n'
            "  exit 125\n"
            "fi\n"
            'echo "deadbeefcafe0123"\n'
        )
        script.chmod(0o755)
        return script, counter

    @staticmethod
    def _sandbox(monkeypatch, script, retry_seconds="0"):
        from robothor.engine.sandbox import Sandbox, SandboxMode

        monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", str(script))
        monkeypatch.setenv("ROBOTHOR_SANDBOX_START_RETRY_SECONDS", retry_seconds)
        return Sandbox(mode=SandboxMode.DOCKER, run_id=uuid.uuid4().hex, workspace="/tmp")

    @pytest.mark.asyncio
    async def test_one_transient_failure_is_retried_and_the_sandbox_starts(
        self, tmp_path, monkeypatch
    ):
        script, counter = self._fake_binary(tmp_path, fail_times=1)
        sb = self._sandbox(monkeypatch, script)

        await sb.start()

        assert sb.container_id == "deadbeefcafe0123"
        assert sb._started is True
        assert counter.read_text().strip() == "2", "expected exactly two invocations"

    @pytest.mark.asyncio
    async def test_a_second_failure_raises_the_original_error_class(self, tmp_path, monkeypatch):
        script, counter = self._fake_binary(tmp_path, fail_times=2)
        sb = self._sandbox(monkeypatch, script)

        with pytest.raises(RuntimeError) as excinfo:
            await sb.start()

        assert "newuidmap" in str(excinfo.value), "the runtime's own stderr must survive"
        assert sb._started is False
        assert counter.read_text().strip() == "2", (
            "one bounded retry, not a loop — a third attempt means the bound is gone"
        )

    @pytest.mark.asyncio
    async def test_retries_can_be_turned_off(self, tmp_path, monkeypatch):
        """The knob is real, and 0 restores the pre-fix single attempt."""
        script, counter = self._fake_binary(tmp_path, fail_times=1)
        monkeypatch.setenv("ROBOTHOR_SANDBOX_START_RETRIES", "0")
        sb = self._sandbox(monkeypatch, script)

        with pytest.raises(RuntimeError):
            await sb.start()

        assert counter.read_text().strip() == "1"


class TestRetryCleansUpTheNameItLeft:
    """The retry reuses the identical argv, `--name sandbox-<run_id[:12]>` and all.

    A container runtime can create the container and THEN fail (network setup,
    the uid_map write, a hook). Attempt 2 then dies on

        Error: creating container storage: the container name
        "sandbox-abc123" is already in use

    which is not the fault the operator needs to read: the retry converts a
    diagnosable "newuidmap: Operation not permitted" into a name collision
    that says nothing about why the first attempt failed. So the name is
    cleaned up between attempts, and the final error carries attempt 1's
    stderr alongside the last one.
    """

    @staticmethod
    def _fake_binary(tmp_path, fail_times: int):
        """A runtime that owns its name: `run` refuses a duplicate, `rm` frees it."""
        counter = tmp_path / "runs"
        held = tmp_path / "name-held"
        script = tmp_path / "fake-podman"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "rm" ]; then\n'
            f'  rm -f "{held}"\n'
            "  exit 0\n"
            "fi\n"
            f'if [ -f "{held}" ]; then\n'
            '  echo "Error: creating container storage: the container name is already in use" >&2\n'
            "  exit 125\n"
            "fi\n"
            f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
            "n=$((n+1))\n"
            f'echo "$n" > "{counter}"\n'
            # The container exists from here on, even if the start then fails.
            f'touch "{held}"\n'
            f'if [ "$n" -le {fail_times} ]; then\n'
            '  echo "attempt $n: newuidmap: write to uid_map failed: Operation not permitted" >&2\n'
            "  exit 125\n"
            "fi\n"
            'echo "deadbeefcafe0123"\n'
        )
        script.chmod(0o755)
        return script, counter

    @staticmethod
    def _sandbox(monkeypatch, script):
        from robothor.engine.sandbox import Sandbox, SandboxMode

        monkeypatch.setenv("ROBOTHOR_SANDBOX_BINARY", str(script))
        monkeypatch.setenv("ROBOTHOR_SANDBOX_START_RETRY_SECONDS", "0")
        return Sandbox(mode=SandboxMode.DOCKER, run_id=uuid.uuid4().hex, workspace="/tmp")

    @pytest.mark.asyncio
    async def test_the_retry_frees_the_name_the_first_attempt_took(self, tmp_path, monkeypatch):
        script, counter = self._fake_binary(tmp_path, fail_times=1)
        sb = self._sandbox(monkeypatch, script)

        await sb.start()

        assert sb.container_id == "deadbeefcafe0123", (
            "attempt 2 collided with the name attempt 1 left behind"
        )
        assert counter.read_text().strip() == "2"

    @pytest.mark.asyncio
    async def test_the_final_error_carries_the_first_attempts_stderr(self, tmp_path, monkeypatch):
        script, _ = self._fake_binary(tmp_path, fail_times=2)
        sb = self._sandbox(monkeypatch, script)

        with pytest.raises(RuntimeError) as excinfo:
            await sb.start()

        message = str(excinfo.value)
        assert "attempt 1:" in message, (
            "the first failure is the diagnosable one and must survive the retry"
        )
        assert "attempt 2:" in message
        assert sb._started is False


class TestTheRetryDelayIsBounded:
    """A typo in the delay must not wedge every run that wants a sandbox.

    The retry sits inside `Sandbox.start`, which the runner awaits before the
    loop begins and under no deadline of its own. An operator writing
    `ROBOTHOR_SANDBOX_START_RETRY_SECONDS=3000` (or leaving a millisecond
    value in) would stall every containerised run for the better part of an
    hour, on the failure path, with the run's own clock already running.
    """

    def test_the_delay_is_clamped_above(self, monkeypatch):
        from robothor.engine.sandbox import MAX_START_RETRY_SECONDS, start_retry_seconds

        monkeypatch.setenv("ROBOTHOR_SANDBOX_START_RETRY_SECONDS", "3000")
        assert start_retry_seconds() == MAX_START_RETRY_SECONDS

    def test_a_sane_value_is_untouched(self, monkeypatch):
        from robothor.engine.sandbox import start_retry_seconds

        monkeypatch.setenv("ROBOTHOR_SANDBOX_START_RETRY_SECONDS", "5")
        assert start_retry_seconds() == 5.0

    def test_a_negative_value_becomes_no_wait(self, monkeypatch):
        from robothor.engine.sandbox import start_retry_seconds

        monkeypatch.setenv("ROBOTHOR_SANDBOX_START_RETRY_SECONDS", "-1")
        assert start_retry_seconds() == 0.0
