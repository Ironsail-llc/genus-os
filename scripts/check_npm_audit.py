#!/usr/bin/env python3
"""Gate CI on `npm audit --json` using an explicit, tracked advisory allowlist.

`npm audit --audit-level=high` fails outright on ANY high/critical advisory
with no way to acknowledge a specific one as reviewed and accepted. Some
advisories live inside third-party packages we don't control and can't patch
(see .npm-audit-allowlist.json for the current, named cases) -- a blanket
--audit-level=high gate blocks CI forever on those, which is worse than the
gate it's meant to enforce.

This script re-implements the gate as a named allowlist: any HIGH or CRITICAL
severity advisory whose GHSA id is not explicitly listed in the allowlist
file fails the check. Every allowlist entry must name the advisory id, the
package it lives in, and a reason. Advisories NOT in the file still fail the
gate -- nothing is exempted by default, and a newly reported issue (even in
an already-allowlisted package) fails until it is reviewed and added by id.

Usage:
    python3 scripts/check_npm_audit.py <path-to-npm-audit-json>
    python3 scripts/check_npm_audit.py <path-to-npm-audit-json> --allowlist <path>

Exit codes:
    0 - no high/critical advisory outside the allowlist
    1 - at least one high/critical advisory is not allowlisted; the input
        report/allowlist could not be read or parsed; or the report doesn't
        look like a successful audit run (e.g. `npm audit` itself errored --
        see report_shape_error) -- failures fail closed, never silently pass
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST_PATH = REPO_ROOT / ".npm-audit-allowlist.json"
GATED_SEVERITIES = {"high", "critical"}
GHSA_RE = re.compile(r"GHSA-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+")


def load_allowlist(path: Path) -> dict[str, dict]:
    """Load the tracked allowlist file into a {ghsa_id: entry} map.

    A missing file is treated as an empty allowlist (fail closed on any
    high/critical advisory) rather than an error, so the check still works
    before the file exists.
    """
    if not path.exists():
        return {}
    entries = json.loads(path.read_text())
    return {entry["id"]: entry for entry in entries}


def extract_ghsa_id(via: dict) -> str | None:
    """Pull the GHSA id out of an advisory's url (or any string field)."""
    match = GHSA_RE.search(via.get("url", ""))
    if match:
        return match.group(0)
    for value in via.values():
        if isinstance(value, str):
            match = GHSA_RE.search(value)
            if match:
                return match.group(0)
    return None


def report_shape_error(report: dict) -> str | None:
    """Return why `report` isn't a trustworthy audit report, or None if it's fine.

    `npm audit --json` writes valid JSON even when the audit itself failed --
    e.g. a network blip against the registry exits 1 but still prints
    something like {"message": "...ECONNREFUSED...", "error": {...}}, with
    NO "vulnerabilities" key. Piped through the workflow's `|| true`, that
    would otherwise read as "zero vulnerabilities found" and silently pass
    the gate. Fail closed instead: reject an explicit "error" key outright,
    and require both "vulnerabilities" and "metadata" -- both present on
    every real, successful `npm audit --json` run.
    """
    if not isinstance(report, dict):
        return "report is not a JSON object"
    if "error" in report:
        return f"npm audit reported an error: {report['error']!r}"
    if "vulnerabilities" not in report:
        return "no 'vulnerabilities' key -- not a valid npm audit report"
    if "metadata" not in report:
        return "no 'metadata' key -- not a valid npm audit report"
    return None


def gated_advisories(report: dict) -> list[tuple[str, dict]]:
    """Return (package_name, advisory) for every high/critical advisory.

    `npm audit --json`'s `vulnerabilities.<pkg>.via` mixes advisory objects
    (dicts, with their own `severity`) with plain strings that are just
    cross-references to other vulnerable packages in the same report --
    those aren't advisories and are skipped.
    """
    found = []
    for package_name, vuln in report.get("vulnerabilities", {}).items():
        for via in vuln.get("via", []):
            if not isinstance(via, dict):
                continue
            if via.get("severity") in GATED_SEVERITIES:
                found.append((package_name, via))
    return found


def check(
    report: dict, allowlist: dict[str, dict]
) -> tuple[list[tuple[str, str | None, dict]], set[str]]:
    """Split gated advisories into (violations, allowed_ids)."""
    violations: list[tuple[str, str | None, dict]] = []
    allowed_ids: set[str] = set()
    for package_name, via in gated_advisories(report):
        ghsa_id = extract_ghsa_id(via)
        entry = allowlist.get(ghsa_id) if ghsa_id else None
        if entry is None:
            violations.append((package_name, ghsa_id, via))
        else:
            allowed_ids.add(ghsa_id)
    return violations, allowed_ids


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_path", type=Path, help="npm audit --json output file")
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST_PATH,
        help="path to the tracked advisory allowlist (default: %(default)s)",
    )
    args = parser.parse_args(argv[1:])

    try:
        report = json.loads(args.report_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: could not read/parse {args.report_path}: {exc}", file=sys.stderr)
        return 1

    shape_error = report_shape_error(report)
    if shape_error is not None:
        print(
            f"FAIL: {args.report_path} does not look like a successful npm audit "
            f"report -- {shape_error}. Failing closed rather than treating an "
            "audit-tool failure as zero vulnerabilities.",
            file=sys.stderr,
        )
        return 1

    try:
        allowlist = load_allowlist(args.allowlist)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: could not read/parse {args.allowlist}: {exc}", file=sys.stderr)
        return 1

    violations, allowed_ids = check(report, allowlist)

    for ghsa_id in sorted(allowed_ids):
        entry = allowlist[ghsa_id]
        print(f"ALLOWED {ghsa_id} ({entry['package']}): {entry['reason']} [added {entry['added']}]")

    if violations:
        print(f"FAIL: {len(violations)} high/critical advisory(ies) not in {args.allowlist}:")
        for package_name, ghsa_id, via in violations:
            title = via.get("title", "<no title>")
            url = via.get("url", "<no url>")
            print(f"  - {package_name}: {ghsa_id or '<unknown id>'} - {title} ({url})")
        return 1

    print(f"OK: no high/critical npm advisories outside {args.allowlist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
