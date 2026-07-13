#!/usr/bin/env python3
"""Fail when a GitHub workflow executes an action through a mutable ref."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW_ROOT = Path(".github/workflows")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^#\s]+)")
IMMUTABLE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    failures: list[str] = []
    workflow_paths = sorted((*WORKFLOW_ROOT.glob("*.yml"), *WORKFLOW_ROOT.glob("*.yaml")))
    for path in workflow_paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = USES.match(line)
            if match is None:
                continue
            target = match.group(1)
            if target.startswith("./"):
                continue
            action, separator, revision = target.rpartition("@")
            if not separator or "/" not in action or IMMUTABLE_COMMIT.fullmatch(revision) is None:
                failures.append(f"{path}:{line_number}: {target}")

    if failures:
        print("External workflow actions must be pinned to full 40-character commit SHAs:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"All external actions are immutably pinned across {len(workflow_paths)} workflows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
