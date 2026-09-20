"""Disposable PostgreSQL resources for reproducible cross-revision benchmarks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    binary = next(
        (
            p
            for p in (Path("/usr/lib/postgresql/18/bin"), Path("/usr/lib/postgresql/16/bin"))
            if (p / "initdb").exists()
        ),
        None,
    )
    if binary is None:
        found = shutil.which("initdb")
        if not found:
            parser.error("PostgreSQL binaries are required")
        binary = Path(found).parent

    with tempfile.TemporaryDirectory(prefix="runtime-pg-") as directory:
        root = Path(directory)
        data, socket = root / "data", root / "socket"
        socket.mkdir()

        def command(name, *values):
            subprocess.run([str(binary / name), *map(str, values)], check=True, capture_output=True)

        command("initdb", "-D", data, "-U", "runtime_test", "--auth=trust", "--no-locale")
        command(
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
            command("createdb", "-h", socket, "-U", "runtime_test", "robothor_runtime_test")
            env = {
                key: value
                for key, value in os.environ.items()
                if not any(word in key for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
            }
            env.update(
                ROBOTHOR_TEST_DB_DSN=f"dbname=robothor_runtime_test user=runtime_test host={socket}",
                ROBOTHOR_DB_NAME="robothor_runtime_test",
                ROBOTHOR_DB_USER="runtime_test",
                ROBOTHOR_DB_HOST=str(socket),
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bench.interactive.run_runner",
                    "--samples",
                    str(args.samples),
                    "--output",
                    str(args.output.resolve()),
                ],
                cwd=args.checkout,
                env=env,
                check=False,
            )
        finally:
            command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
