"""Boot the real daemon with an empty fleet and private database/Redis resources."""

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from urllib.request import urlopen

# Loaded only in the drill daemon and its Python children. Provider connections
# and external executables are refused, not redirected to shared services.
GUARD = """import os, socket, subprocess, sys
from pathlib import Path
root = os.environ["RUNTIME_DRILL_ROOT"]
original = socket.socket.connect
def connect(self, address):
    if isinstance(address, str) and address.startswith(root + "/"):
        return original(self, address)
    raise PermissionError("drill refuses external network")
socket.socket.connect = connect
popen = subprocess.Popen
class PrivatePopen(popen):
    def __init__(self, args, *rest, **kwargs):
        allowed = isinstance(args, (list, tuple)) and len(args) >= 3 and args[0] == sys.executable and list(args[1:3]) == ["-m", "robothor.engine.calendar_recovery_worker"]
        if not allowed:
            raise PermissionError("drill refuses external executable")
        with open(root + "/worker-spawns.jsonl", "a") as stream:
            import json
            stream.write(json.dumps(list(args)) + "\\n")
        super().__init__(args, *rest, **kwargs)
subprocess.Popen = PrivatePopen
"""


def run(root, database_env, *, resume=False, goal_phase=None):
    root = root / "daemon-drill"
    root.mkdir()
    workspace = root / "workspace"
    (workspace / "docs/agents").mkdir(parents=True)
    (workspace / "docs/workflows").mkdir()
    (root / "owner.yaml").write_text("{}\n")
    guard = root / "guard"
    guard.mkdir()
    extra = ""
    if goal_phase:
        from bench.runtime.daemon_goal_crash import MANIFEST

        (workspace / "docs/agents/main.yaml").write_text(MANIFEST)
        extra = "\nfrom bench.runtime.daemon_goal_crash import install\ninstall()\n"
    (guard / "sitecustomize.py").write_text(GUARD + extra)
    redis_socket = root / "redis.sock"
    redis = subprocess.Popen(
        [
            "redis-server",
            "--port",
            "0",
            "--unixsocket",
            str(redis_socket),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    daemon = None
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {key: value for key, value in database_env.items() if key.startswith("ROBOTHOR_DB_")}
        env.update(
            {
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(guard) + os.pathsep + str(Path.cwd()),
                "PYTHONUNBUFFERED": "1",
                "RUNTIME_DRILL_ROOT": str(root),
                "ROBOTHOR_WORKSPACE": str(workspace),
                "ROBOTHOR_OWNER_CONFIG": str(root / "owner.yaml"),
                "ROBOTHOR_DEFAULT_TENANT": "default",
                "ROBOTHOR_TENANT_ID": "default",
                "ROBOTHOR_ENGINE_HOST": "127.0.0.1",
                "ROBOTHOR_ENGINE_PORT": str(port),
                "GENUS_ENVIRONMENT": "test",
                "GENUS_INSECURE_DEV_MODE": "true",
                "REDIS_URL": f"unix://{redis_socket}?db=15",
            }
        )
        if goal_phase:
            env["RUNTIME_GOAL_PHASE"] = goal_phase
        if resume:
            env["ROBOTHOR_RESUME_IN_FLIGHT"] = "true"
        with (root / "daemon.log").open("w") as log:
            daemon = subprocess.Popen(
                [sys.executable, "-m", "robothor.engine.daemon"],
                env=env,
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            expires = time.monotonic() + 30
            while time.monotonic() < expires and daemon.poll() is None:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    "Daemon did not become healthy: " + (root / "daemon.log").read_text()[-6000:]
                )
            if goal_phase:
                from bench.runtime.daemon_goal_crash import await_state

                await_state(root, daemon, database_env, goal_phase)
            started = time.monotonic()
            if goal_phase == "crash":
                os.killpg(daemon.pid, signal.SIGKILL)
            else:
                daemon.send_signal(signal.SIGTERM)
            code = daemon.wait(timeout=15)
            assert code == (-signal.SIGKILL if goal_phase == "crash" else 0), (
                root / "daemon.log"
            ).read_text()[-6000:]
            log_text = (root / "daemon.log").read_text()
            assert "All subsystems started" in log_text, log_text[-6000:]
            if resume:
                assert "Resume scan failed" not in log_text, log_text[-6000:]
                assert "Startup resume failed" not in log_text, log_text[-6000:]
                assert "Checkpoint schema mismatch" not in log_text, log_text[-6000:]
            assert (root / "worker-spawns.jsonl").exists(), log_text[-6000:]
            spawns = (root / "worker-spawns.jsonl").read_text().splitlines()
            assert "Calendar recovery sweep deferred" not in log_text, log_text[-6000:]
            try:
                os.killpg(daemon.pid, 0)
            except ProcessLookupError:
                pass
            else:
                if goal_phase != "crash":
                    raise AssertionError("Daemon left a process in its owned group")
                # A killed subprocess may remain a zombie until the host reaps
                # it, but no executable member of this owned group may survive.
                for stat in Path("/proc").glob("[0-9]*/stat"):
                    try:
                        fields = stat.read_text().rsplit(")", 1)[1].split()
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    assert int(fields[2]) != daemon.pid or fields[0] == "Z", (
                        "Crash left a live owned process: " + str(stat)
                    )
            assert spawns, "Recovery worker was not started"
            return {
                "health_ready": True,
                "shutdown_exit_code": code,
                "shutdown_seconds": time.monotonic() - started,
                "recovery_worker_spawns": len(spawns),
            }
    finally:
        if daemon is not None:
            with suppress(ProcessLookupError):
                os.killpg(daemon.pid, signal.SIGKILL)
            daemon.wait()
        redis.terminate()
        redis.wait(timeout=5)
