#!/usr/bin/env python3
"""Render ``docs/reference/configuration.md`` from the settings registry.

The reference is generated, never edited: a hand-written configuration page is
how 91 of the platform's variables ended up documented nowhere.
``tests/test_configuration_doc_generated.py`` asserts the committed file equals
this script's output, so changing a description without regenerating fails CI.

Usage::

    python scripts/gen_configuration_doc.py          # write the file
    python scripts/gen_configuration_doc.py --check  # exit 1 if it is stale
    python scripts/gen_configuration_doc.py --stdout # print, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "reference" / "configuration.md"

#: One sentence per group, in the order the model declares them.
GROUP_BLURBS = {
    "paths": "Where the instance keeps its files. Every path defaults under the workspace.",
    "database": "PostgreSQL connection, tenancy and row-level security.",
    "redis": "Redis connection and the event-bus streams it carries.",
    "ollama": "The local Ollama endpoint and the models served from it.",
    "providers": "Cloud model routing, budgets and the failure controls around them.",
    "engine": "The agent execution layer: bind address, concurrency, pacing, sandbox.",
    "channels": "How the instance reaches people, and who it says it is.",
    "auth": "Who may reach the bridge and the dashboard, and how that is proven.",
    "flags": (
        "Guardrails and feature gates. Ones marked governed are inventoried in "
        "`infra/flags.yaml` with an owner and a promotion date."
    ),
    "services": "Side services the instance runs: ports, endpoints and their knobs.",
    "secrets": "Secret material that belongs to no single service.",
    "substrate": "Where and how the instance runs: host accounts, federation, backups.",
    "ops": (
        "Backups, restores, SLO probes, alert delivery and the volume guard — "
        "read by shell, not by Python, which is why they were the last thing "
        "anyone declared."
    ),
}

HEADER = """<!--
GENERATED FILE — do not edit.

Rendered from robothor/settings/model.py by scripts/gen_configuration_doc.py.
Change a setting there and re-run the script; the committed file and the
generator output are compared in tests/test_configuration_doc_generated.py.
-->

# Configuration reference

Every `ROBOTHOR_*` and `GENUS_*` setting Genus OS reads, with the exact
environment variable name.

Provider credentials that carry no Genus prefix — `OPENROUTER_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and their peers — are **not** in this
table. They belong to the providers, not to this platform, so they are
declared and documented with the provider integration that reads them rather
than renamed into a Genus namespace.

Settings resolve from four places, lowest priority first:

1. the defaults below,
2. the `settings:` block of `<workspace>/.robothor/config.yaml`,
3. the environment,
4. an explicit runtime override.

`<workspace>` is `$ROBOTHOR_WORKSPACE`, or `~/robothor` when that is unset.

Column meanings:

- **Restart** — what a change waits on. `no` means it is picked up without a
  restart; `next run` means the next invocation of the script or timer that
  reads it; anything else names the systemd units to restart. The units are
  declared on the setting itself, so `genus config set` prints the same
  answer this table does.
- **Secret** — holds a credential. These never have a default and are redacted
  by `genus config`.
- **Since** — the release that introduced the setting. `legacy` predates this
  registry.

Run `genus config schema` for the same information as JSON Schema.
"""

FOOTER = """
## Deprecated names

These older names are still read, but each emits one `DeprecationWarning` per
process naming its replacement. They stop being read after two minor releases.

"""


def _default_cell(record: dict) -> str:
    if record["secret"]:
        return "_(unset)_"
    default = record["default"]
    if default is None or default == "":
        return "_(empty)_"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    return f"`{default}`"


def _restart_cell(record: dict) -> str:
    """What a change to this setting waits on, from its own declaration."""
    if not record["restart_required"]:
        return "no"
    units = record["restart_units"] or ()
    return ", ".join(f"`{unit}`" for unit in units) if units else "next run"


def _escape(text: str) -> str:
    """Make a description safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render() -> str:
    """The full contents of docs/reference/configuration.md."""
    sys.path.insert(0, str(REPO_ROOT))
    from robothor.settings.aliases import DEPRECATED_ALIASES
    from robothor.settings.registry import field_index, groups

    index = field_index()
    by_group: dict[str, list[dict]] = {group: [] for group in groups()}
    for env_name, record in index.items():
        if record["env"] == env_name:
            by_group[record["group"]].append(record)

    lines = [HEADER]
    total = sum(len(records) for records in by_group.values())
    lines.append(f"\n{total} settings in {len(by_group)} groups.\n")

    for group, records in by_group.items():
        lines.append(f"\n## {group}\n")
        blurb = GROUP_BLURBS.get(group)
        if blurb:
            lines.append(f"\n{blurb}\n")
        lines.append("\n| Variable | Type | Default | Restart | Secret | Since | Description |\n")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |\n")
        for record in sorted(records, key=lambda item: item["env"]):
            description = _escape(record["description"])
            if record["governed"]:
                description = f"**governed.** {description}"
            if record["aliases"]:
                also = ", ".join(f"`{alias}`" for alias in record["aliases"])
                description = f"{description} Also read from {also}."
            lines.append(
                "| `{env}` | {type} | {default} | {restart} | {secret} | {since} | {desc} |\n".format(
                    env=record["env"],
                    type=record["type"],
                    default=_default_cell(record),
                    restart=_restart_cell(record),
                    secret="yes" if record["secret"] else "no",
                    since=record["since"],
                    desc=description,
                )
            )

    lines.append(FOOTER)
    lines.append("| Deprecated name | Use instead |\n")
    lines.append("| --- | --- |\n")
    for old, new in sorted(DEPRECATED_ALIASES.items()):
        lines.append(f"| `{old}` | `{new}` |\n")

    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the committed file is stale"
    )
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args(argv)

    rendered = render()
    if args.stdout:
        sys.stdout.write(rendered)
        return 0
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT} is stale — run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
