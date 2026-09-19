"""Locked, durable deployment of an immutable local engine release.

Runs independently of the engine being restarted. State distinguishes tested,
deployed and verified, and a failed readiness check restores the old release.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

STATE = Path("/run/robothor/local-deployment.json")
LOCK = Path("/run/robothor/local-deployment.lock")
OVERRIDE = Path("/etc/systemd/system/robothor-engine.service.d/zzzzzz-local-integration.conf")

HOST_OVERRIDE = Path("/etc/systemd/system/robothor-host-exec.service.d/release.conf")


def command(argv: list[str], **kwargs: Any) -> str:
    return str(subprocess.check_output(argv, text=True, **kwargs)).strip()


def save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def install_override(content: str, path: Path = OVERRIDE) -> None:
    with tempfile.NamedTemporaryFile(mode="w") as stream:
        stream.write(content)
        stream.flush()
        subprocess.run(["sudo", "-n", "install", "-m", "0644", stream.name, str(path)], check=True)


def wait_idle(seconds: int = 900) -> None:
    from robothor.engine_control import control_request

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = control_request("GET", "/api/runs/active")
        if result.get("runs") == []:
            return
        time.sleep(3)
    raise RuntimeError("Engine did not become idle; no release was switched")


def verify_live(snapshot: Path, seconds: int = 180) -> dict[str, Any]:
    from robothor.engine_control import control_request

    deadline = time.monotonic() + seconds
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            control_request("GET", "/health/startup")
            result = control_request("POST", "/api/admin/capabilities/probe", timeout=60)
            if not result.get("ready"):
                raise RuntimeError("Live computer capability probe failed: " + json.dumps(result))
            if not Path(result["host_execution"]["loaded_module"]).is_relative_to(snapshot):
                raise RuntimeError("Host service loaded a different release")
            if not Path(result["loaded_module"]).is_relative_to(snapshot):
                raise RuntimeError("Engine loaded a different release")
            return result
        except Exception as exc:
            error = exc
            time.sleep(3)
    raise RuntimeError(f"Candidate never became ready: {error}")


def deploy(workspace: Path, revision: str, job_path: Path) -> dict[str, Any]:
    job = json.loads(job_path.read_text()) if job_path.exists() else {}
    job.update(status="prepared", requested_revision=revision)
    save(job_path, job)
    switched = False
    previous = ""
    previous_host = ""
    lock = LOCK.open("r" if LOCK.exists() else "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        shared = json.loads(STATE.read_text())
        commit = command(
            ["git", "-C", str(workspace), "rev-parse", "--verify", revision + "^{commit}"]
        )
        # Refuse to erase concurrent work. The caller must integrate the
        # release that is live at lock acquisition, not a stale snapshot.
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "merge-base",
                "--is-ancestor",
                shared["combined_commit"],
                commit,
            ],
            check=True,
        )
        if not re.fullmatch("[0-9a-f]{40}", commit):
            raise RuntimeError("Invalid resolved revision")
        snapshot = Path.home() / ".local/share/robothor/combined-preview" / commit[:10]
        if not snapshot.exists():
            snapshot.mkdir(parents=True)
            with tempfile.TemporaryFile() as archive:
                subprocess.run(
                    ["git", "-C", str(workspace), "archive", commit], stdout=archive, check=True
                )
                archive.seek(0)
                with tarfile.open(fileobj=archive) as bundle:
                    bundle.extractall(snapshot, filter="data")
            (snapshot / "venv").symlink_to(
                Path(shared["snapshot"]) / "venv", target_is_directory=True
            )
            (snapshot / "REVISION").write_text(commit + "\n")
        elif (snapshot / "REVISION").read_text().strip() != commit:
            raise RuntimeError("Snapshot revision collision")
        # Test the EXACT snapshot, with production database writes prohibited.
        test_env = {**os.environ, "PYTHONPATH": str(snapshot), "ROBOTHOR_DB_NAME": "robothor_test"}
        # Runtime releases intentionally omit pytest. Use the instance's dev
        # interpreter against the candidate's exact code, then import the
        # entrypoints with the runtime interpreter before any service switch.
        test_python = os.environ.get("ROBOTHOR_DEPLOY_TEST_PYTHON") or str(
            workspace / "venv/bin/python"
        )
        with (job_path.parent / "tests.log").open("w") as test_log:
            subprocess.run(
                [
                    test_python,
                    "-m",
                    "pytest",
                    "-q",
                    "robothor/engine/tests/test_task_continuity.py",
                    "robothor/engine/tests/test_host_execution.py",
                    "robothor/engine/tests/test_plan_integrity.py",
                    "robothor/engine/tests/test_browser.py",
                    "robothor/engine/tests/test_desktop_tools.py",
                    "robothor/engine/tests/test_local_deploy.py",
                    "robothor/engine/tests/test_repair_recovery.py",
                ],
                cwd=snapshot,
                env=test_env,
                stdout=test_log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=300,
            )
        subprocess.run(
            [
                str(snapshot / "venv/bin/python"),
                "-c",
                "import robothor.engine.daemon; import robothor.engine.host_execution",
            ],
            cwd=snapshot,
            env={**os.environ, "PYTHONPATH": str(snapshot)},
            check=True,
            timeout=60,
        )
        job.update(status="tested", revision=commit, snapshot=str(snapshot))
        save(job_path, job)
        wait_idle()
        previous = OVERRIDE.read_text()
        previous_host = HOST_OVERRIDE.read_text() if HOST_OVERRIDE.exists() else "[Service]\n"
        new = (
            "[Service]\nWorkingDirectory=" + str(snapshot) + "\n"
            "Environment=PYTHONPATH=" + str(snapshot) + "\n"
            "Environment=ROBOTHOR_WORKSPACE=" + str(workspace) + "\n"
            "Environment=ROBOTHOR_HOST_EXEC_SOCKET=/run/robothor-host/exec.sock\n"
            "ExecStart=\nExecStart="
            + str(snapshot / "venv/bin/python")
            + " -m robothor.engine.daemon\n"
        )
        job["previous_override"] = previous
        save(job_path, job)
        install_override(new)
        switched = True
        host_new = (
            "[Service]\nWorkingDirectory=" + str(snapshot) + "\n"
            "Environment=PYTHONPATH=" + str(snapshot) + "\n"
            "ExecStart=\nExecStart="
            + str(snapshot / "venv/bin/python")
            + " -m robothor.engine.host_execution\n"
        )
        install_override(host_new, HOST_OVERRIDE)
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], check=True)
        subprocess.run(["sudo", "-n", "systemctl", "restart", "robothor-host-exec"], check=True)
        subprocess.run(["sudo", "-n", "systemctl", "restart", "robothor-engine"], check=True)
        job["status"] = "deployed"
        save(job_path, job)
        evidence = verify_live(snapshot)
        shared.update(
            combined_commit=commit,
            snapshot=str(snapshot),
            status="computer capabilities verified",
            owner="host-task-continuity",
            engine_restart_pending=False,
        )
        shared["computer_repair"] = {
            "job": str(job_path),
            "revision": commit,
            "verified_at": time.time(),
            "evidence": evidence,
        }
        save(STATE, shared)
        job.update(status="verified", evidence=evidence)
        save(job_path, job)
        if job.get("continuation", {}).get("task_context"):
            from robothor.engine_control import control_request

            try:
                job["resume"] = control_request(
                    "POST", "/api/admin/repairs/" + job["job_id"] + "/resume"
                )
            except Exception as exc:
                job["resume_error"] = str(exc)
                save(job_path, job)
        return job
    except Exception as exc:
        job.update(status="failed", error=str(exc))
        if switched:
            # Rollback occurs while we own the deployment lock as well. The
            # candidate never becomes the shared combined_commit until verified.
            install_override(previous_host, HOST_OVERRIDE)
            install_override(previous)
            subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], check=True)
            subprocess.run(["sudo", "-n", "systemctl", "restart", "robothor-host-exec"], check=True)
            subprocess.run(["sudo", "-n", "systemctl", "restart", "robothor-engine"], check=True)
            job["rolled_back"] = True
        save(job_path, job)
        raise
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def queue(workspace: str, revision: str, context: dict[str, Any]) -> dict[str, Any]:
    import uuid

    job_id = str(uuid.uuid4())
    path = Path(workspace) / "local/repairs" / job_id / "state.json"
    save(
        path,
        {
            "job_id": job_id,
            "status": "queued",
            "continuation": context,
            "requested_revision": revision,
        },
    )
    with path.with_name("deployment.log").open("w") as log:
        # A separate systemd unit outlives both the engine and host service.
        subprocess.run(
            [
                "sudo",
                "-n",
                "systemd-run",
                "--quiet",
                "--collect",
                "--unit=robothor-repair-" + job_id,
                "--uid=" + str(os.getuid()),
                "--property=WorkingDirectory=" + str(Path(__file__).resolve().parents[2]),
                "--setenv=ROBOTHOR_WORKSPACE=" + workspace,
                "--setenv=PYTHONPATH=" + str(Path(__file__).resolve().parents[2]),
                sys.executable,
                "-m",
                "robothor.engine.local_deploy",
                "--workspace",
                workspace,
                "--revision",
                revision,
                "--job",
                str(path),
            ],
            stdout=log,
            stderr=log,
            check=True,
        )
    return {
        "job_id": job_id,
        "status": "queued",
        "state_path": str(path),
        "continuation": "Deployment will wait for this run to finish. Preserve the original "
        "task as pending; check genus-host status " + job_id + " after restart.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    deploy(args.workspace, args.revision, args.job)


if __name__ == "__main__":
    main()
