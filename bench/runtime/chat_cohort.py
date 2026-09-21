"""Repeat the accepted chat status/pause workflow in isolated test processes.

The native test supplies private goal/task stores and synthetic business tools.
Its live option uses only the already selected model chain. Every attempt and
failure is retained; a process loss leaves an unresolved journal start.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from bench.runtime.chat_cohort_report import report
from bench.runtime.native_journal import NativeJournal


def collect(manifest, directory, samples=30):
    if samples < 30:
        raise ValueError("At least 30 repetitions are required for this cohort")
    configured = yaml.safe_load(manifest.read_text())["model"]
    selected = {
        "primary": configured["primary"],
        "fallbacks": configured.get("fallbacks", []),
        "temperature": configured.get("temperature", 0.5),
    }
    model = selected["primary"]
    directory.mkdir(parents=True, exist_ok=False)
    snapshot = directory / "model-settings.yaml"
    with snapshot.open("x") as stream:
        yaml.safe_dump({"model": selected}, stream)
    journal = NativeJournal(directory / "cohort.jsonl")
    root = Path(__file__).resolve().parents[2]
    for index in range(samples):
        output = directory / f"sample-{index:03d}.json"
        log = directory / f"sample-{index:03d}.log"
        settings = {"manifest": str(snapshot.resolve()), "output": str(output.resolve())}
        journal.record("sample_started", model, index)
        with log.open("x") as stream:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "robothor/engine/tests/test_runtime_chat_goal_acceptance.py",
                    "-k",
                    "single-goal",
                ],
                cwd=root,
                env={**os.environ, "ROBOTHOR_RUNTIME_CHAT_LIVE": json.dumps(settings)},
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        journal.record(
            "sample_finished",
            model,
            index,
            exit_code=result.returncode,
            artifact=output.name if output.exists() else None,
            log=log.name,
        )
    return directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    directory = collect(args.manifest, args.output_directory)
    result = report(directory)
    with (directory / "summary.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result))
    return 0 if result["screening_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
