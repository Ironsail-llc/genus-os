"""Skill library maintenance commands.

``robothor skills migrate-state`` — one-shot, idempotent migration that
moves runtime keys (usage_count, last_used, state) out of each tracked
``agents/skills/<name>/meta.json`` into a gitignored ``state.json``
sidecar. Safe to re-run; existing sidecars win over legacy meta values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def cmd_skills(args: argparse.Namespace) -> int:
    command = getattr(args, "skills_command", None)

    if command == "migrate-state":
        from robothor.engine.skills import migrate_skill_runtime_state

        result = migrate_skill_runtime_state()
        for name in result["migrated"]:
            print(f"  migrated:  {name}")
        for name in result["errors"]:
            print(f"  ERROR:     {name} (unreadable meta.json — left untouched)")
        print(
            f"migrate-state: {len(result['migrated'])} migrated, "
            f"{len(result['unchanged'])} already clean, "
            f"{len(result['errors'])} errors"
        )
        return 1 if result["errors"] else 0

    print("Usage: robothor skills migrate-state")
    return 1
