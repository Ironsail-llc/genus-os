"""Agents must be able to restart services without sudo.

NoNewPrivileges blocks every setuid path, sudo included — which is the point,
because the engine user has passwordless sudo and six agents hold unrestricted
`exec`. But it also blocks the one legitimate use we found in 30 days of
history: an agent running `sudo systemctl restart robothor-engine` to heal the
fleet.

The operator's standing rule is that Robothor does things itself. So the
capability is preserved without the escalation path: the agent writes a trigger
file, and a systemd .path unit runs the restart as root. systemd holds the
privilege; the agent never gains it.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PATH_UNIT = REPO_ROOT / "infra/systemd/robothor-restart.path"
SVC_UNIT = REPO_ROOT / "infra/systemd/robothor-restart.service"
TELEGRAM_MODULE = REPO_ROOT / "robothor/engine/telegram.py"
TRIGGER = "/run/robothor/restart-request"


def test_path_unit_watches_the_trigger_file():
    assert PATH_UNIT.exists(), "robothor-restart.path missing"
    src = PATH_UNIT.read_text()
    assert TRIGGER in src, f"path unit must watch {TRIGGER}"
    assert "PathExists=" in src or "PathModified=" in src


def test_restart_service_runs_as_root_and_is_oneshot():
    assert SVC_UNIT.exists(), "robothor-restart.service missing"
    src = SVC_UNIT.read_text()
    assert "Type=oneshot" in src
    # No User= line means root — systemd holds the privilege, not the agent.
    assert "\nUser=" not in src, (
        "the restart unit must run as root; that is the whole point — the agent "
        "never gains privilege, systemd exercises it"
    )
    # The restart itself moved into the root-owned handler when the broker
    # grew from one hardcoded unit to an allowlist. Follow it there rather
    # than dropping the assertion.
    handler = SVC_UNIT.parent.parent / "bin" / "robothor-restart-handler.sh"
    assert handler.exists(), "the handler the unit executes is missing"
    assert "systemctl restart" in handler.read_text()


def test_restart_service_consumes_the_trigger():
    """A trigger left in place would restart in a loop."""
    handler = (SVC_UNIT.parent.parent / "bin" / "robothor-restart-handler.sh").read_text()
    assert "rm -f" in handler, "the trigger file must be removed, or the path unit re-fires forever"
    # And it must be removed for REFUSED requests too, or an un-allowlisted
    # name loops the path unit just as effectively as an honoured one.
    consume = handler.index("rm -f")
    refuse = handler.index("not in the allowlist")
    assert consume < refuse, "the request must be consumed before it is judged"


def test_restart_service_only_restarts_robothor_units():
    """The agent must not be able to name an arbitrary unit."""
    handler = (SVC_UNIT.parent.parent / "bin" / "robothor-restart-handler.sh").read_text()
    allowed = handler.split("ALLOWED=(")[1].split(")")[0]
    names = [n.strip() for n in allowed.split() if n.strip()]
    assert names, "the allowlist is empty"
    assert all(n.startswith("robothor-") for n in names), (
        f"a non-robothor unit is agent-restartable: {names}"
    )
    # The unit name must come from the FILENAME, never the file's contents.
    assert "$(cat" not in handler and "$(<" not in handler, (
        "the unit to restart must NOT be read from the agent-writable trigger "
        "file — that would let an agent restart (or stop) any unit on the box"
    )


def test_handler_writes_the_trigger_not_sudo():
    """The Telegram /restart handler must use the trigger-file mechanism
    above, not shell out to `sudo systemd-run`.

    Regression guard for the silent no-op: under the live NoNewPrivileges
    sandbox, `sudo -n systemd-run ...` launched via Popen with all fds
    DEVNULL and never awaited dies unseen in the child — the handler replied
    "Restarting..." and then did nothing. This asserts the handler region
    contains no "sudo" at all, so that failure mode cannot come back.
    """
    src = TELEGRAM_MODULE.read_text()
    match = re.search(
        r"async def _handle_restart_command\(.*?\n(?=    async def |\Z)",
        src,
        re.DOTALL,
    )
    assert match, "_handle_restart_command not found in robothor/engine/telegram.py"
    handler_src = match.group(0)
    assert "sudo" not in handler_src.lower(), (
        "the restart handler must not shell out to sudo — it must write the "
        "trigger file that robothor-restart.path watches"
    )
    assert "systemd-run" not in handler_src, (
        "the restart handler must not invoke systemd-run directly — that path "
        "is owned by robothor-restart.service, triggered via the trigger file"
    )
    assert "_RESTART_TRIGGERS" in handler_src, (
        "the restart handler must consult the injectable _RESTART_TRIGGERS map"
    )


# ── The broker restarts each unit once ───────────────────────────────────────
#
# A restart job that arrives while the same unit's previous restart is still
# starting it SUPERSEDES that start job, and systemd kills whatever the job was
# running. On the engine that is ExecStartPre=load-secrets.sh, a control
# process, whose death by SIGTERM is not a clean exit: `Control process exited,
# code=killed, status=15/TERM` → `Failed with result 'signal'` → OnFailure=
# pages (2026-09-17, on a deploy). The broker must therefore never be the
# second restart: it issues ONE `systemctl restart` for everything requested,
# leaves out any unit that already has a job queued, and holds a lock so two
# handlers cannot interleave.

HANDLER = REPO_ROOT / "infra/bin/robothor-restart-handler.sh"


def install_fake_systemctl(tmp_path: Path) -> Path:
    """A systemctl stand-in. Records every call; answers `show -p Job --value
    <unit>` from <tmp>/jobs, one `<unit> <id>` line per queued job."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    jobs = tmp_path / "jobs"
    jobs.touch()
    (bindir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        'if [ "$1" = show ]; then\n'
        '    unit="${*: -1}"\n'
        f'    awk -v u="$unit" \'$1 == u {{ $1 = ""; sub(/^ /, ""); print }}\' "{jobs}"\n'
        "fi\n"
        "exit 0\n"
    )
    (bindir / "systemctl").chmod(0o755)
    # logger would write to the real syslog; a no-op keeps the test hermetic.
    (bindir / "logger").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "logger").chmod(0o755)
    return log


def handler_env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "requests").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "PATH": f"{tmp_path / 'bin'}:{env['PATH']}",
            "ROBOTHOR_RESTART_REQUEST_DIR": str(tmp_path / "requests"),
            "ROBOTHOR_RESTART_LEGACY_REQUEST": str(tmp_path / "restart-request"),
            "ROBOTHOR_RESTART_LOCK": str(tmp_path / "restart.lock"),
        }
    )
    return env


def request(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "requests" / name
    path.parent.mkdir(exist_ok=True)
    path.touch()
    return path


def run_handler(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HANDLER)],
        capture_output=True,
        text=True,
        timeout=30,
        env=handler_env(tmp_path),
    )


def restart_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [
        line.split()[1:] for line in log.read_text().splitlines() if line.startswith("restart ")
    ]


def test_several_requests_become_one_restart_transaction(tmp_path: Path):
    log = install_fake_systemctl(tmp_path)
    request(tmp_path, "robothor-engine")
    request(tmp_path, "robothor-bridge")
    (tmp_path / "restart-request").touch()  # the legacy trigger also means the engine

    result = run_handler(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = restart_calls(log)
    assert len(calls) == 1, f"expected one restart transaction, got {calls}"
    assert sorted(calls[0]) == ["robothor-bridge.service", "robothor-engine.service"], (
        "each requested unit once — the legacy trigger and the engine request are ONE restart"
    )
    assert not list((tmp_path / "requests").iterdir())
    assert not (tmp_path / "restart-request").exists()


def test_a_unit_with_a_queued_job_is_left_alone(tmp_path: Path):
    """The job already queued for it will start it with the new code; a
    second restart on top would kill that job's ExecStartPre and page."""
    log = install_fake_systemctl(tmp_path)
    (tmp_path / "jobs").write_text("robothor-engine.service 4242\n")
    request(tmp_path, "robothor-engine")
    request(tmp_path, "robothor-bridge")

    result = run_handler(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert restart_calls(log) == [["robothor-bridge.service"]]
    assert "robothor-engine.service" in result.stderr and "4242" in result.stderr, (
        "a skipped unit must be reported, not silently dropped"
    )
    assert not list((tmp_path / "requests").iterdir()), "requests are consumed either way"


def test_nothing_to_restart_calls_nothing(tmp_path: Path):
    log = install_fake_systemctl(tmp_path)
    (tmp_path / "jobs").write_text("robothor-engine.service 7\n")
    request(tmp_path, "robothor-engine")

    result = run_handler(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert restart_calls(log) == []


def test_a_refused_name_is_consumed_and_never_restarted(tmp_path: Path):
    log = install_fake_systemctl(tmp_path)
    request(tmp_path, "sshd")
    request(tmp_path, "robothor-bridge")

    result = run_handler(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert restart_calls(log) == [["robothor-bridge.service"]]
    assert "sshd" in result.stderr
    assert not list((tmp_path / "requests").iterdir())


def test_two_handlers_serialise_on_the_lock(tmp_path: Path):
    """While one handler holds the lock the next one waits — it neither
    consumes the requests nor calls systemctl until the first is done."""
    log = install_fake_systemctl(tmp_path)
    req = request(tmp_path, "robothor-engine")
    env = handler_env(tmp_path)
    lock = Path(env["ROBOTHOR_RESTART_LOCK"]).open("w")  # noqa: SIM115 - released explicitly below
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        proc = subprocess.Popen(
            ["bash", str(HANDLER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                assert proc.poll() is None, "the handler finished while the lock was held"
                time.sleep(0.05)
            assert req.exists(), "the request was consumed while another handler held the lock"
            assert restart_calls(log) == [], (
                "systemctl was called while another handler held the lock"
            )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
        out, err = proc.communicate(timeout=20)
    finally:
        lock.close()

    assert proc.returncode == 0, out + err
    assert restart_calls(log) == [["robothor-engine.service"]]
    assert not req.exists()
