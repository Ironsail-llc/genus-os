#!/usr/bin/env python3
"""Deterministic per-person aggregation step of the devops report pipeline.

Reads /tmp/devops_github_data.json + /tmp/devops_jira_data.json, joins all
author/assignee/reviewer handles to CRM people via contact_identifiers, and
writes /tmp/devops_people_rollup.json. Anomalies (unresolved handles,
roster engineers with zero activity, bot authors) are surfaced as separate
top-level lists — nothing is silently dropped.

Called by the devops-report-pipeline workflow between the collectors and
the analyst. The analyst narrates this output; it no longer attempts to
build per-person rollups from raw collector data.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.constants import DEFAULT_TENANT
from robothor.engine.reports.devops_aggregate import (
    PostgresAggregateDAL,
    aggregate_people,
)

GITHUB_IN = Path("/tmp/devops_github_data.json")
JIRA_IN = Path("/tmp/devops_jira_data.json")
OUT = Path("/tmp/devops_people_rollup.json")
CONFIG = Path(__file__).resolve().parent.parent / "brain" / "memory" / "devops-config.json"


def _load_engineering_role_keywords() -> list[str] | None:
    """Read `engineering_roles` from devops-config.json if present.

    Returning None tells aggregate_people to use its defaults
    (Engineer / Developer / SRE / DevOps).
    """
    if not CONFIG.exists():
        return None
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        return None
    raw = cfg.get("engineering_roles")
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    return None


def main() -> int:
    if not GITHUB_IN.exists():
        print(f"missing input: {GITHUB_IN}", file=sys.stderr)
        return 1
    if not JIRA_IN.exists():
        print(f"missing input: {JIRA_IN}", file=sys.stderr)
        return 1

    github_data = json.loads(GITHUB_IN.read_text())
    jira_data = json.loads(JIRA_IN.read_text())

    tenant_id = os.environ.get("ROBOTHOR_DEFAULT_TENANT", DEFAULT_TENANT)
    engineer_keywords = _load_engineering_role_keywords()

    from robothor.db.connection import get_connection

    with get_connection() as conn:
        dal = PostgresAggregateDAL(conn, tenant_id)
        rollup = aggregate_people(
            github_data,
            jira_data,
            dal,
            engineer_role_keywords=engineer_keywords,
        )

    OUT.write_text(json.dumps(rollup, indent=2, default=str))

    n_people = len(rollup["people"])
    n_missing = len(rollup["missing_from_roster"])
    n_unres = len(rollup["unresolved_handles"])
    n_bots = len(rollup["bots_filtered"])
    print(
        f"Rollup: {n_people} people active, {n_missing} on roster with no activity, "
        f"{n_unres} unresolved handles, {n_bots} bot authors → {OUT}"
    )
    if n_unres:
        print("Unresolved handles (need contact_identifiers rows):", file=sys.stderr)
        for u in rollup["unresolved_handles"][:10]:
            print(
                f"  - {u['channel']}: {u['identifier']} ({u['occurrences']} occurrences)",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
