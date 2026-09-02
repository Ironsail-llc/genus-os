#!/usr/bin/env python3
"""Swap the fleet's model chain across every agent manifest, safely.

Why this exists: switching the fleet to ``ox-alpha`` took seven failed attempts
on 2026-08-22. The manifests were only half the story. Three things have to
agree, and missing any one of them leaves the change looking applied while the
old model keeps answering:

1. every agent manifest's ``model.primary`` / ``model.fallbacks`` (including
   ``docs/agents/delphi/``, a SUBDIRECTORY that a top-level glob misses),
2. ``chat_sessions.model_override`` — a session-level pin beats the manifest, so
   a stale override silently ignores every manifest edit. Delphi's Telegram
   session was pinned to mimo-v2.5 and never switched no matter how many times
   the manifests were rewritten,
3. an engine restart, because manifests load at startup.

``fallbacks`` gets its own flag because a change to ``_defaults.yaml`` does NOT
propagate: ``_deep_merge`` REPLACES lists rather than extending them, so an
agent that names any fallbacks at all keeps exactly the ones in its own file.
The chain has to be written into each manifest.

Defaults to a dry run. Nothing is written unless --apply is passed.

    scripts/set-fleet-model.py --fallbacks "openrouter/a/b,ollama_chat/c:27b"
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

import yaml

WORKSPACE = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))
AGENT_GLOBS = ("docs/agents/*.yaml", "docs/agents/*/*.yaml")

#: ``model.primary`` on its own line. Rewritten textually rather than by
#: round-tripping YAML: these manifests carry comments that explain WHY a model
#: was chosen, and a yaml.safe_load/dump cycle silently deletes all of them.
_PRIMARY = re.compile(r"^(?P<indent>\s*)primary:\s*(?P<model>\S+)\s*$", re.MULTILINE)

#: A ``fallbacks:`` key, either style the fleet uses:
#:   inline flow — ``  fallbacks: ["ollama_chat/qwen3.8:27b"]  # offline tier``
#:   block seq   — ``  fallbacks:`` followed by ``    - model`` lines
_FALLBACKS = re.compile(r"^(?P<indent>[ \t]*)fallbacks:(?P<rest>.*)$")
_INLINE_LIST = re.compile(r"^\s*\[(?P<items>[^]]*)\]\s*(?P<comment>#.*)?$")
_BLOCK_ITEM = re.compile(r"^(?P<indent>[ \t]+)-\s*(?P<value>.*)$")

#: A scalar safe to leave unquoted inside a BLOCK sequence. ``:`` is allowed
#: there only because it is never followed by a space in a model path
#: (``ollama_chat/qwen3.8:27b``); in FLOW context it is always quoted.
_PLAIN_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+:-]*$")


#: Never touched. ``retired/`` agents are not scheduled, and agents like
#: pf-watchdog (RETIRED 2026-08-27) ran a deliberately LOCAL model
#: (ollama_chat/qwen3:8b) that has
#: nothing to do with which hosted model the fleet is on. A blanket rewrite
#: would silently drag both onto the cloud model. ``.archived/`` is the same
#: story with a leading dot — and pathlib's ``*`` DOES match dotted dirs, so
#: it has to be named here.
_SKIP_DIRS = ("retired", ".archived")


def manifests(*, include_defaults: bool = False) -> list[Path]:
    """Every live agent manifest, delphi's subdirectory included.

    ``_defaults.yaml`` is excluded unless asked for: editing it is a no-op for
    fallbacks (lists are replaced, not merged) and a real change for anything
    an agent leaves unset, so it should be a deliberate choice.
    """
    skip_stems = {"schema"} if include_defaults else {"schema", "_defaults"}
    found: list[Path] = []
    for pattern in AGENT_GLOBS:
        found.extend(WORKSPACE.glob(pattern))
    return sorted(
        {p for p in found if p.stem not in skip_stems and p.parent.name not in _SKIP_DIRS}
    )


def _render(value: str, *, flow: bool) -> str:
    """Render one model id as a YAML scalar for the style it lands in."""
    if not flow and _PLAIN_SAFE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def rewrite_fallbacks_text(text: str, fallbacks: list[str]) -> tuple[str | None, int]:
    """Return ``(new text or None, count of occurrences left alone)``.

    The text is None when nothing changed (no such key, or already correct).
    Each occurrence keeps its own style — an inline flow list stays inline, a
    block sequence stays a block — so the diff is the list and nothing else.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    changed = False
    unhandled = 0
    while i < len(lines):
        raw = lines[i]
        body = raw.rstrip("\r\n")
        eol = raw[len(body) :] or "\n"
        match = _FALLBACKS.match(body)
        if not match:
            out.append(raw)
            i += 1
            continue

        indent = match.group("indent")
        rest = match.group("rest")
        inline = _INLINE_LIST.match(rest)
        if inline:
            items = ", ".join(_render(f, flow=True) for f in fallbacks)
            comment = inline.group("comment")
            suffix = f"  {comment}" if comment else ""
            new = f"{indent}fallbacks: [{items}]{suffix}{eol}"
            changed = changed or new != raw
            out.append(new)
            i += 1
            continue

        stripped = rest.strip()
        if stripped and not stripped.startswith("#"):
            # An anchor, an alias, a plain scalar — something this tool does
            # not model. Left exactly as it is, and COUNTED, because a chain
            # that quietly missed a manifest is the failure mode this whole
            # script exists to prevent.
            unhandled += 1
            out.append(raw)
            i += 1
            continue

        # Block sequence: keep the key line verbatim, replace the item lines.
        comment = f"  {stripped}" if stripped else ""
        key_line = f"{indent}fallbacks: []{comment}{eol}" if not fallbacks else raw
        out.append(key_line)
        changed = changed or key_line != raw
        i += 1
        item_indent = f"{indent}  "
        old_items: list[str] = []
        while i < len(lines):
            item = _BLOCK_ITEM.match(lines[i].rstrip("\r\n"))
            if not item or len(item.group("indent")) <= len(indent):
                break
            item_indent = item.group("indent")
            old_items.append(lines[i])
            i += 1
        # An empty block sequence is not a YAML list; it parses as null. The
        # only valid way to write "no fallbacks" is the flow form, so an empty
        # chain collapses the key line above and drops the items.
        new_items = [f"{item_indent}- {_render(f, flow=False)}{eol}" for f in fallbacks]
        changed = changed or new_items != old_items
        out.extend(new_items)

    return ("".join(out) if changed else None), unhandled


def _verify(original: str, updated: str, fallbacks: list[str]) -> str | None:
    """Return an error string if the rewrite did not do what it claims.

    Textual edits to YAML are exactly the kind of change that can look right
    and parse wrong, so the result is parsed and every ``fallbacks`` value it
    contains is checked before anything is written.
    """
    try:
        yaml.safe_load(original)
    except yaml.YAMLError:
        return None  # was not parseable before us; not ours to judge
    try:
        data = yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        return f"rewrite produced invalid YAML: {exc}"

    def walk(node: object) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "fallbacks" and value != fallbacks:
                    return f"fallbacks did not take: {value!r}"
                if (err := walk(value)) is not None:
                    return err
        elif isinstance(node, list):
            for item in node:
                if (err := walk(item)) is not None:
                    return err
        return None

    return walk(data)


def rewrite_primary(
    path: Path, new_primary: str, only_from: str, *, apply: bool
) -> tuple[str, str] | None:
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
    if apply:
        path.write_text(updated)
    return old, new_primary


def rewrite_fallbacks(path: Path, fallbacks: list[str], *, apply: bool) -> str | None:
    """Apply the chain to one manifest. Returns a status line, or None if unchanged."""
    text = path.read_text()
    if not any(_FALLBACKS.match(line) for line in text.splitlines()):
        return "no fallbacks: line — chain NOT applied"
    updated, unhandled = rewrite_fallbacks_text(text, fallbacks)
    if unhandled:
        return (
            f"{unhandled} fallbacks: value(s) in a style this tool does not "
            "rewrite (anchor/alias?) — chain NOT applied, edit by hand"
        )
    if updated is None:
        return None
    error = _verify(text, updated, fallbacks)
    if error:
        return f"SKIPPED — {error}"
    if apply:
        path.write_text(updated)
    return "fallbacks -> " + ", ".join(fallbacks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="only_from", help="only swap agents currently on THIS model")
    parser.add_argument("--primary", help="e.g. openrouter/xiaomi/mimo-v2.5")
    parser.add_argument(
        "--fallbacks",
        help="comma-separated fallback chain, widest first (empty string clears it)",
    )
    parser.add_argument(
        "--include-defaults",
        action="store_true",
        help="also rewrite _defaults.yaml (lists are REPLACED on merge, so this "
        "changes nothing for agents that name their own fallbacks)",
    )
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    if args.primary is None and args.fallbacks is None:
        parser.error("nothing to do: pass --primary and/or --fallbacks")
    if args.primary is not None and not args.only_from:
        parser.error("--primary requires --from (the model being swapped away from)")

    fallbacks = (
        [f.strip() for f in args.fallbacks.split(",") if f.strip()]
        if args.fallbacks is not None
        else None
    )

    changed = 0
    for path in manifests(include_defaults=args.include_defaults):
        rel = path.relative_to(WORKSPACE)
        touched = False
        if args.primary is not None:
            result = rewrite_primary(path, args.primary, args.only_from, apply=args.apply)
            if result:
                old, new = result
                print(f"  {rel}: {old} -> {new}")
                touched = True
        if fallbacks is not None:
            note = rewrite_fallbacks(path, fallbacks, apply=args.apply)
            if note:
                print(f"  {rel}: {note}")
                touched = touched or note.startswith("fallbacks")
        if touched:
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
