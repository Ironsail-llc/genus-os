"""Run tracking integration checks on a disposable, canonically migrated cluster.

Invoke with the repository Python environment: python -m bench.runtime.migrated_integration.
No shared database is read, migrated or modified.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2

from robothor.db.migrate import apply


def main():
    binary = Path("/usr/lib/postgresql/16/bin")
    if not (binary / "initdb").exists():
        raise RuntimeError("PostgreSQL 16 with pgvector is required for this local drill")
    with tempfile.TemporaryDirectory(prefix="runtime-migrated-") as directory:
        root = Path(directory)
        data, socket = root / "data", root / "socket"
        socket.mkdir()

        def pg(name, *args):
            subprocess.run(
                [str(binary / name), *map(str, args)], check=True, capture_output=True, text=True
            )

        pg(
            "initdb",
            "-D",
            data,
            "-U",
            "runtime_test",
            "--auth=trust",
            "--no-locale",
            "--encoding=UTF8",
        )
        pg(
            "pg_ctl",
            "-D",
            data,
            "-l",
            root / "postgres.log",
            "-o",
            f"-F -h '' -k {socket}",
            "-w",
            "start",
        )
        try:
            pg("createdb", "-h", socket, "-U", "runtime_test", "runtime_modernization_test")
            dsn = f"dbname=runtime_modernization_test user=runtime_test host={socket}"
            with psycopg2.connect(dsn) as conn:
                applied = apply(connection=conn)
                assert {
                    "127_runtime_contract",
                    "138_goal_provider_reservations",
                    "139_goal_task_family_controls",
                } <= set(applied)
                assert apply(connection=conn) == []
            pg("createdb", "-h", socket, "-U", "runtime_test", "runtime_upgrade_test")
            from bench.runtime.populated_upgrade import upgrade

            with psycopg2.connect(
                f"dbname=runtime_upgrade_test user=runtime_test host={socket}"
            ) as conn:
                report = upgrade(conn, root)
                print("POPULATED_UPGRADE " + json.dumps(report), flush=True)
            env = {
                **os.environ,
                "ROBOTHOR_DB_HOST": str(socket),
                "ROBOTHOR_DB_NAME": "runtime_modernization_test",
                "ROBOTHOR_DB_USER": "runtime_test",
                "ROBOTHOR_DB_PASSWORD": "",
                "ROBOTHOR_DB_PORT": "5432",
                "ROBOTHOR_TEST_DB_DSN": dsn,
                "ROBOTHOR_DEFAULT_TENANT": "default",
            }
            if "--daemon" in sys.argv:
                from bench.runtime.daemon_drill import run

                print("DAEMON_DRILL " + json.dumps(run(root, env)), flush=True)
                if "--restart" in sys.argv:
                    from bench.runtime.restart_state import seed, verify

                    identifiers = seed(dsn)
                    results = []
                    for cycle in range(2):
                        location = root / f"restart-{cycle}"
                        location.mkdir()
                        results.append(run(location, env, resume=True))
                        verify(dsn, identifiers)
                    print("STOPPED_RESTART " + json.dumps(results), flush=True)
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "robothor/engine/tests/test_run_identity_is_persisted.py",
                    "robothor/engine/tests/test_runner_person_link.py",
                    "bench/runtime/test_resume_family.py",
                ],
                env=env,
                check=False,
            ).returncode
        finally:
            pg("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


if __name__ == "__main__":
    raise SystemExit(main())
