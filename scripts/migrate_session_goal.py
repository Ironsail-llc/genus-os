#!/usr/bin/env python3
"""One-shot migration: brain/GOAL.md → crm_tasks row.

Reads the legacy markdown goal file (created by the May-8 Codex work) and
materialises it as a ``crm_task`` with the ``session_goal`` tag. Existing
free-form evidence is preserved as ``note`` kind — it does not satisfy the
new completion guard (which requires structured ``test_run`` + ``commit``
evidence), but it is not discarded.

Idempotent: if an active session-goal task already exists for the scope,
the script reports and exits 0 without re-creating it.

Usage:
    python scripts/migrate_session_goal.py [--workspace PATH] [--tenant TENANT] \\
        [--agent AGENT_ID] [--dry-run]

Rollback:
    The legacy file is moved to brain/GOAL.md.legacy.<utc-stamp>.bak.
    To roll back: ``mv brain/GOAL.md.legacy.<stamp>.bak brain/GOAL.md`` and
    delete the created task: ``UPDATE crm_tasks SET deleted_at = NOW()
    WHERE id = '<task-id>';``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add parent dir to import robothor when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.constants import DEFAULT_TENANT  # noqa: E402
from robothor.crm import dal  # noqa: E402
from robothor.engine.session_goal import _resolve_workspace, regenerate_goal_md_cache  # noqa: E402

logger = logging.getLogger("migrate_session_goal")


def parse_legacy_goal(text: str) -> dict[str, Any]:
    """Parse the legacy GOAL.md format Codex shipped on 2026-05-08.

    The legacy file has a flat frontmatter (key: value pairs) followed by
    ``## Section`` blocks: Objective, Success Criteria, Evidence, Completion
    Note, Agent Instruction. Returns a dict with normalised fields:

      {
        "objective": str,
        "success_criteria": list[str],
        "evidence_lines": list[str],
        "completion_note": str,
      }

    Anything we can't parse cleanly is dropped — the dest schema is
    structured, so legacy free-form rows are best preserved as ``note``
    evidence with a generic summary.
    """
    sections: dict[str, list[str]] = {
        "objective": [],
        "success_criteria": [],
        "evidence": [],
        "completion_note": [],
    }
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line == "# Active Goal":
            continue
        if line.startswith("## "):
            current = line[3:].strip().lower().replace(" ", "_")
            continue
        if current and current in sections:
            sections[current].append(line)

    objective = "\n".join(sections["objective"]).strip()

    criteria: list[str] = []
    for line in sections["success_criteria"]:
        item = line[2:].strip() if line.startswith("- ") else line.strip()
        if item:
            criteria.append(item)

    evidence_lines: list[str] = []
    for line in sections["evidence"]:
        item = line[2:].strip() if line.startswith("- ") else line.strip()
        if not item or item == "none yet":
            continue
        evidence_lines.append(item)

    completion = "\n".join(sections["completion_note"]).strip()

    return {
        "objective": objective,
        "success_criteria": criteria,
        "evidence_lines": evidence_lines,
        "completion_note": completion,
    }


def migrate(
    workspace: Path,
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str = "",
    dry_run: bool = False,
) -> int:
    legacy_path = workspace / "brain" / ("GOAL.md" if not agent_id else f"goals/{agent_id}.md")
    if not legacy_path.exists():
        print(f"[skip] no legacy file at {legacy_path}")
        return 0

    existing = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if existing:
        print(
            f"[skip] active session-goal task already exists "
            f"(id={existing['id']}); leaving {legacy_path} alone"
        )
        return 0

    text = legacy_path.read_text(encoding="utf-8")
    parsed = parse_legacy_goal(text)
    if not parsed["objective"]:
        print(f"[error] could not parse objective from {legacy_path}")
        return 2

    if dry_run:
        print("[dry-run] would create session_goal task with:")
        print(f"  objective = {parsed['objective'][:120]!r}")
        print(f"  success_criteria = {parsed['success_criteria']!r}")
        print(f"  evidence (as 'note') = {parsed['evidence_lines']!r}")
        print(f"  completion_note = {parsed['completion_note'][:120]!r}")
        return 0

    task_id = dal.create_session_goal(
        tenant_id=tenant_id,
        objective=parsed["objective"],
        success_criteria=parsed["success_criteria"]
        or ["The feature behaviour matches the requested objective."],
        agent_id=agent_id,
    )
    if not task_id:
        print("[error] dal.create_session_goal returned None")
        return 3

    for line in parsed["evidence_lines"]:
        if ":" in line:
            kind, _, summary = line.partition(":")
        else:
            kind, summary = "legacy", line
        kind = kind.strip()
        summary = summary.strip() or kind
        # Coerce to the dest enum: anything legacy → 'note'.
        ok = dal.add_session_goal_evidence(
            task_id=task_id,
            kind="note",
            summary=f"[legacy:{kind}] {summary}",
            reference="",
            valid=True,
            tenant_id=tenant_id,
        )
        if not ok:
            print(f"[warn] failed to copy legacy evidence: {line!r}")

    # Move the legacy file aside. We keep it on disk so manual rollback is
    # trivial and so the file is not silently lost.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = legacy_path.with_suffix(legacy_path.suffix + f".legacy.{stamp}.bak")
    legacy_path.rename(backup)

    # Regenerate the read-cache from the task so brain/GOAL.md is back in
    # place pointing at the new source of truth.
    regenerate_goal_md_cache(tenant_id=tenant_id, workspace=workspace, agent_id=agent_id)

    print(f"[ok] created session-goal task {task_id}")
    print(f"     legacy file moved to {backup}")
    print(f"     read-cache regenerated at {legacy_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace path (default: ROBOTHOR_WORKSPACE env or ~/robothor)",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        help=f"Tenant ID (default: {DEFAULT_TENANT})",
    )
    parser.add_argument(
        "--agent",
        default="",
        help="Agent ID for an agent-scoped goal (default: workspace goal)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without writing to the DB or moving the file",
    )
    args = parser.parse_args(argv)

    workspace = _resolve_workspace(args.workspace)
    return migrate(
        workspace,
        tenant_id=args.tenant,
        agent_id=args.agent,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
