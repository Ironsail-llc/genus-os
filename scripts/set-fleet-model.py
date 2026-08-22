#!/usr/bin/env python3
"""Swap the fleet's primary model across every agent manifest, safely.

Why this exists: switching the fleet to ``ox-alpha`` took seven failed attempts
on 2026-08-22. The manifests were only half the story. Three things have to
agree, and missing any one of them leaves the change looking applied while the
old model keeps answering:

1. every agent manifest's ``model.primary`` (including ``docs/agents/delphi/``,
   a SUBDIRECTORY that a top-level glob misses),
2. ``chat_sessions.model_override`` — a session-level pin beats the manifest, so
   a stale override silently ignores every manifest edit. Delphi's Telegram
   session was pinned to mimo-v2.5 and never switched no matter how many times
   the manifests were rewritten,
3. an engine restart, because manifests load at startup.

Defaults to a dry run. Nothing is written unless --apply is passed.

    scripts/set-fleet-model.py --from openrouter/stealth/ox-alpha \
                               --primary openrouter/xiaomi/mimo-v2.5
    scripts/set-fleet-model.py --from openrouter/stealth/ox-alpha \
                               --primary openrouter/xiaomi/mimo-v2.5 --apply

After --apply, restart the engines so they reload:

    sudo systemctl restart robothor-engine robothor-delphi-engine
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

WORKSPACE = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))
AGENT_GLOBS = ("docs/agents/*.yaml", "docs/agents/*/*.yaml")

#: ``model.primary`` on its own line. Rewritten textually rather than by
#: round-tripping YAML: these manifests carry comments that explain WHY a model
#: was chosen, and a yaml.safe_load/dump cycle silently deletes all of them.
_PRIMARY = re.compile(r"^(?P<indent>\s*)primary:\s*(?P<model>\S+)\s*$", re.MULTILINE)


#: Never touched. ``retired/`` agents are not scheduled, and agents like
#: pf-watchdog run a deliberately LOCAL model (ollama_chat/qwen3:8b) that has
#: nothing to do with which hosted model the fleet is on. A blanket rewrite
#: would silently drag both onto the cloud model.
_SKIP_DIRS = ("retired",)


def manifests() -> list[Path]:
    found: list[Path] = []
    for pattern in AGENT_GLOBS:
        found.extend(WORKSPACE.glob(pattern))
    return sorted(
        {
            p
            for p in found
            if p.stem not in ("schema", "_defaults") and p.parent.name not in _SKIP_DIRS
        }
    )


def rewrite(path: Path, new_primary: str, only_from: str) -> tuple[str, str] | None:
    """Return (old, new) when this manifest's primary changes, else None.

    ``only_from`` is required: swapping only agents currently ON the outgoing
    model is what keeps a fleet swap from dragging deliberately-different
    agents (a local Ollama watchdog, a pinned analyst) onto it too.
    """
    text = path.read_text()
    match = _PRIMARY.search(text)
    if not match:
        return None
    old = match.group("model")
    if old != only_from or old == new_primary:
        return None
    updated = (
        text[: match.start()]
        + f"{match.group('indent')}primary: {new_primary}"
        + text[match.end() :]
    )
    path.write_text(updated) if REWRITE else None
    return old, new_primary


REWRITE = False


def main() -> int:
    global REWRITE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from", dest="only_from", required=True, help="only swap agents currently on THIS model"
    )
    parser.add_argument("--primary", required=True, help="e.g. openrouter/xiaomi/mimo-v2.5")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    REWRITE = args.apply

    changed = 0
    for path in manifests():
        result = rewrite(path, args.primary, args.only_from)
        if result:
            old, new = result
            print(f"  {path.relative_to(WORKSPACE)}: {old} -> {new}")
            changed += 1

    verb = "updated" if args.apply else "would update"
    print(f"\n{verb} {changed} manifest(s)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    print(
        "\nNEXT, or the change will not take effect:\n"
        "  1. clear session pins (a session override beats the manifest):\n"
        "       psql -d robothor_memory -c \\\n"
        '         "UPDATE chat_sessions SET model_override = NULL WHERE model_override IS NOT NULL;"\n'
        "  2. restart the engines (manifests load at startup):\n"
        "       sudo systemctl restart robothor-engine robothor-delphi-engine\n"
        "  3. verify what actually answered:\n"
        "       psql -d robothor_memory -c \\\n"
        '         "SELECT model_used, count(*) FROM agent_runs '
        "WHERE created_at > now()-interval '10 min' GROUP BY 1;\""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
