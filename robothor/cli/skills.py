"""Skill library maintenance commands.

``genus skills migrate-state`` — one-shot, idempotent migration that
moves runtime keys (usage_count, last_used, state) out of each tracked
``agents/skills/<name>/meta.json`` into a gitignored ``state.json``
sidecar. Safe to re-run; existing sidecars win over legacy meta values.

``genus skills migrate-instance`` — one-shot, idempotent migration that
moves every skill an agent created out of the tracked ``agents/skills/``
tree into this instance's own skills directory (``brain/skills`` unless
``ROBOTHOR_INSTANCE_SKILLS_DIR`` says otherwise). Skills the platform
ships stay where they are. Safe to re-run.
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

    if command == "migrate-instance":
        from robothor.engine.skills import bundled_skills_dir, instance_skills_dir
        from robothor.engine.skills import migrate_instance_skills as _migrate

        dry_run = bool(getattr(args, "dry_run", False))
        result = _migrate(dry_run=dry_run)
        verb = "would move" if dry_run else "moved"
        print(f"from: {bundled_skills_dir()}")
        print(f"to:   {instance_skills_dir()}")
        for name in result["moved"]:
            print(f"  {verb}:    {name}")
        for name in result["conflicts"]:
            print(f"  CONFLICT:  {name} (already present in the instance — resolve by hand)")
        for name in result["errors"]:
            print(f"  ERROR:     {name} (unreadable or unmovable — left untouched)")
        print(
            f"migrate-instance: {len(result['moved'])} {verb}, "
            f"{len(result['skipped'])} platform-bundled, "
            f"{len(result['conflicts'])} conflicts, "
            f"{len(result['errors'])} errors"
        )
        return 1 if (result["errors"] or result["conflicts"]) else 0

    print("Usage: genus skills {migrate-state,migrate-instance}")
    return 1
