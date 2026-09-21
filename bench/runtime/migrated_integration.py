"""Run tracking integration checks on a disposable, canonically migrated cluster.

Invoke with the repository Python environment: python -m bench.runtime.migrated_integration.
No shared database is read, migrated or modified.
--chat-browser adds a real Chromium/Next/native-chat recovery drill; build app/ first.
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
                    "140_chat_approval_receipts",
                    "141_runtime_effects",
                    "142_goal_effect_lookup",
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
            if "--native-task-comparison" in sys.argv:
                from bench.runtime.native_task_comparison import drill

                drill(root, env)
            if "--crm-dispatch-ab" in sys.argv:
                from bench.runtime.crm_dispatch_screening import ab_drill

                print("CRM_DISPATCH_AB " + json.dumps(ab_drill(root, env)), flush=True)
            if "--crm-dispatch-screening" in sys.argv:
                from bench.runtime.crm_dispatch_screening import drill

                print("CRM_DISPATCH_SCREENING " + json.dumps(drill(root, env)), flush=True)
            if "--application-rollback" in sys.argv:
                from bench.runtime.application_rollback import drill

                print("APPLICATION_ROLLBACK " + json.dumps(drill(root, env, dsn)), flush=True)
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
            if "--goal-crash" in sys.argv:
                from bench.runtime.daemon_goal_crash import drill

                print("DAEMON_GOAL_CRASH " + json.dumps(drill(root, env)), flush=True)
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "robothor/engine/tests/test_run_identity_is_persisted.py",
                    "robothor/engine/tests/test_runner_person_link.py",
                    "bench/runtime/test_resume_family.py",
                    "robothor/engine/tests/test_native_goal_recovery.py",
                    "robothor/engine/tests/test_native_multiday_goal.py",
                    "robothor/engine/tests/test_native_admission_rollback.py",
                    "robothor/engine/tests/test_native_deadline_recovery.py",
                    "robothor/engine/tests/test_native_classification_timeout.py",
                    "robothor/engine/tests/test_native_automatic_planning.py",
                    *(
                        ["robothor/engine/tests/test_native_task_live.py"]
                        if "--live-task" in sys.argv
                        else []
                    ),
                    "robothor/engine/tests/test_native_goal_report_audit.py",
                    "robothor/engine/tests/test_native_goal_control_failure.py",
                    "robothor/engine/tests/test_native_effect_journal.py",
                    "robothor/engine/tests/test_native_note_recovery.py",
                    "robothor/engine/tests/test_native_task_uncertainty.py",
                    "robothor/engine/tests/test_native_task_dedup_recovery.py",
                    "robothor/engine/tests/test_native_success_receipt.py",
                    "robothor/engine/tests/test_native_task_final_report.py",
                    "robothor/engine/tests/test_native_unfinished_task.py",
                    "robothor/engine/tests/test_native_chat_readback.py",
                    "robothor/engine/tests/test_native_checkpoint_continuation.py",
                    "robothor/engine/tests/test_native_deep_worker_recovery.py",
                    *(
                        ["robothor/engine/tests/test_native_task_chat_screening.py"]
                        if "--task-chat-screening" in sys.argv
                        else []
                    ),
                    *(
                        [
                            "robothor/engine/tests/test_native_plan_browser.py",
                            "robothor/engine/tests/test_native_task_browser.py",
                        ]
                        if "--chat-browser" in sys.argv
                        else []
                    ),
                ],
                env=env,
                check=False,
            ).returncode
        finally:
            pg("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


if __name__ == "__main__":
    raise SystemExit(main())
