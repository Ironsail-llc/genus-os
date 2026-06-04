#!/usr/bin/env python3
"""One-shot seeding: ensure every real agent has a unified goal task.

For each agent manifest in ``docs/agents/*.yaml`` (skipping utility files
like ``_defaults.yaml``, ``schema.yaml``, ``corrective-actions.yaml``),
this script calls ``dal.get_or_create_agent_goal`` which is idempotent:
agents that already have an active goal task are left alone, and missing
agents get one seeded with ``metric_targets`` populated from the
manifest's ``goals:`` block.

Usage:
    python scripts/seed_agent_goals.py [--tenant TENANT] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

# Add repo root to sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.constants import DEFAULT_TENANT  # noqa: E402
from robothor.crm import dal  # noqa: E402

logger = logging.getLogger("seed_agent_goals")

# Files in docs/agents/ that are NOT real agents.
SKIP_FILES: frozenset[str] = frozenset({"_defaults.yaml", "schema.yaml", "corrective-actions.yaml"})


def _load_manifests(agents_dir: Path) -> list[tuple[str, dict]]:
    """Yield (agent_id, manifest_dict) for each real agent manifest."""
    out: list[tuple[str, dict]] = []
    for path in sorted(agents_dir.glob("*.yaml")):
        if path.name in SKIP_FILES:
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            logger.warning("[skip] %s — parse failed: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        agent_id = str(data.get("id") or path.stem).strip()
        if not agent_id:
            logger.warning("[skip] %s — no id", path.name)
            continue
        out.append((agent_id, data))
    return out


def seed(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agents_dir: Path,
    dry_run: bool = False,
) -> int:
    manifests = _load_manifests(agents_dir)
    if not manifests:
        print(f"[error] no manifests in {agents_dir}")
        return 2

    created = 0
    existing = 0
    failed = 0
    for agent_id, manifest in manifests:
        if dry_run:
            row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
            if row:
                print(f"[dry-run skip] {agent_id} — already has goal task {row['id']}")
                existing += 1
            else:
                goals_block = manifest.get("goals") or {}
                target_count = sum(
                    len(v) if isinstance(v, list) else 0
                    for v in (
                        goals_block.values() if isinstance(goals_block, dict) else [goals_block]
                    )
                )
                print(
                    f"[dry-run create] {agent_id} — would seed with {target_count} metric_targets"
                )
                created += 1
            continue

        try:
            row = dal.get_or_create_agent_goal(
                tenant_id=tenant_id,
                agent_id=agent_id,
                manifest=manifest,
            )
        except Exception as exc:
            print(f"[error] {agent_id}: {exc}")
            failed += 1
            continue
        if row is None:
            print(f"[error] {agent_id}: get_or_create returned None")
            failed += 1
            continue
        # Determine if this was newly created vs existing by checking
        # whether the row's session_goal_meta has a non-empty objective
        # the operator has set, or just our placeholder.
        meta = row.get("session_goal_meta") or {}
        objective = (meta.get("objective") or "") if isinstance(meta, dict) else ""
        is_new = "(seeded from manifest" in objective
        if is_new:
            print(f"[ok created] {agent_id} — task {row['id']}")
            created += 1
        else:
            print(f"[ok existing] {agent_id} — task {row['id']}")
            existing += 1

    print(f"\nSummary: created={created}, existing={existing}, failed={failed}")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        help=f"Tenant id (default: {DEFAULT_TENANT})",
    )
    parser.add_argument(
        "--agents-dir",
        default=str(Path(__file__).resolve().parent.parent / "docs" / "agents"),
        help="Path to docs/agents/ directory",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args(argv)

    return seed(
        tenant_id=args.tenant,
        agents_dir=Path(args.agents_dir),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
