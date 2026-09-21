"""Compare existing installation manifest parsing without executing agents.

Both source revisions use the same read-only installation files and a scrubbed
environment. Raw instructions, prompts, credentials and full config values are
never included in the report. This does not qualify live profile execution.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

WORKER = """
import hashlib, json, sys
from dataclasses import asdict
from pathlib import Path
from robothor.engine import config
assert Path(config.__file__).resolve().is_relative_to(Path.cwd())
workspace = Path(sys.argv[1])
result = {}
for trigger in ("manual", "telegram", "cron"):
    agent = config.load_agent_config("main", workspace / "docs/agents", workspace=workspace, trigger_type=trigger)
    assert agent is not None
    values = asdict(agent)
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
    result[trigger] = {
        "fields": {key: digest(value) for key, value in values.items()},
        "selected": {key: values[key] for key in ("model_primary", "model_fallbacks", "temperature", "timeout_seconds", "max_iterations", "task_protocol")},
        "tools_count": len(values["tools_allowed"]),
    }
print(json.dumps(result))
"""


def compare(baseline, current, workspace):
    files = [
        workspace / "docs/agents/main.yaml",
        workspace / "docs/agents/_defaults.yaml",
        workspace / ".robothor/config.yaml",
    ]

    def input_hashes():
        return {
            str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
            if path.exists()
            else None
            for path in files
        }

    before = input_hashes()
    snapshots = []
    revisions = []
    for source in (baseline, current):
        revisions.append(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        )
        result = subprocess.run(
            [sys.executable, "-c", WORKER, str(workspace)],
            cwd=source,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(source), "PYTHONNOUSERSITE": "1"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("Configuration parsing failed; raw config output withheld")
        snapshots.append(json.loads(result.stdout))
    assert before == input_hashes(), "Installation config changed during comparison"
    differences = {}
    for trigger in snapshots[0]:
        old, new = [snapshot[trigger]["fields"] for snapshot in snapshots]
        differences[trigger] = sorted(
            key for key in old.keys() | new.keys() if old.get(key) != new.get(key)
        )
    return {
        "baseline_revision": revisions[0],
        "current_revision": revisions[1],
        "input_hashes": before,
        "differences": differences,
        "matches": not any(differences.values()),
        "selected_configuration": {
            key: {field: value for field, value in entry.items() if field != "fields"}
            for key, entry in snapshots[1].items()
        },
        "compared_field_counts": {key: len(entry["fields"]) for key, entry in snapshots[1].items()},
        "scope": "Actual installation main manifest/defaults/project config parsed by baseline and current code for manual, telegram and cron triggers. Same scrubbed environment; no runtime environment overrides, prompts, tools, models or agents executed. Full values compared as hashes; only selected non-secret configuration is reported.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = compare(args.baseline.resolve(), Path.cwd(), args.workspace.resolve())
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report))
    return 0 if report["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
