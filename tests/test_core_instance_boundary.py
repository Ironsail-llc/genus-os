"""Core ships what every instance needs. Nothing else.

CLAUDE.md rule #1 applied at the INTEGRATION layer rather than the data
layer. Philip's 2026-08-21 ruling, verbatim: "other people don't
necessarily have Impetus One and other people don't necessarily use
Apollo. Everything should be custom configured."

A framework that carries one operator's CRM vendor, boat sensors and
pharma app is not a framework -- it is one person's instance with extra
steps. The plugin host exists now (#411-#444: six extension groups, hot
install, contract versioning), so an integration leaving core has
somewhere to go.

A RATCHET, not a wall. ``GRANDFATHERED`` records what still sits in core
and may only shrink. Adding an instance-specific integration to core is a
test failure; removing one means deleting its line here.
"""

from __future__ import annotations

import re
from pathlib import Path

HANDLERS = Path(__file__).resolve().parents[1] / "robothor" / "engine" / "tools" / "handlers"

#: Integrations that are one operator's, not every operator's. Each is a
#: known boundary violation awaiting extraction to a plugin.
GRANDFATHERED: set[str] = {
    "jira.py",  # COMMON-BUT-PLUGGABLE: one vendor behind no interface
    "github_api.py",  # COMMON-BUT-PLUGGABLE: same
}

#: Vendors that must not appear in core at all. Apollo was retired by
#: operator decision on 2026-08-21 ("I'm done with Apollo"), not by repair.
BANNED_VENDORS = ("apollo",)


def _handler_files() -> set[str]:
    return {p.name for p in HANDLERS.glob("*.py") if p.name != "__init__.py"}


def test_no_banned_vendor_survives_in_core():
    present = _handler_files()
    offenders = [name for name in present if any(v in name.lower() for v in BANNED_VENDORS)]
    assert not offenders, (
        f"a retired vendor integration is still in core: {offenders}. It was "
        "removed by decision, not by repair -- leaving it means agents are "
        "still offered a tool that only returns errors."
    )


def test_banned_vendors_are_not_dispatched():
    """Deleting the file is not enough if dispatch still imports it."""
    dispatch = (HANDLERS.parent / "dispatch.py").read_text().lower()
    for vendor in BANNED_VENDORS:
        assert vendor not in dispatch, f"dispatch.py still wires {vendor!r} into the tool surface"


def test_banned_vendors_have_no_schemas():
    """A schema is what puts the tool in front of the model."""
    schemas = (HANDLERS.parent / "schemas.py").read_text().lower()
    for vendor in BANNED_VENDORS:
        assert f'"{vendor}_' not in schemas, (
            f"schemas.py still offers {vendor!r} tools to the model"
        )


def test_the_grandfather_list_does_not_go_stale():
    """An extracted integration must leave the list, or the ratchet slips."""
    present = _handler_files()
    gone = GRANDFATHERED - present
    assert not gone, (
        f"these left core -- delete them from GRANDFATHERED so the ratchet "
        f"keeps its tension: {sorted(gone)}"
    )


# ── The grep gate ────────────────────────────────────────────────────────
#
# The handler-file checks above only see the tool surface. A retired vendor
# also leaves prose: dead schema headers, fixture tool names, example URLs,
# flag-manifest soak notes. That residue is how a vendor walks back in --
# someone greps for the name, finds it still in core, and concludes the
# integration is supported. This gate reads every file under the platform
# roots and fails on the name itself.

#: Platform roots. Everything here ships to every instance, so nothing here
#: may name one operator's vendors. ``docs/`` and ``scripts/`` are out of
#: scope on purpose: they carry dated incident write-ups and probe records
#: that are historical fact, not shipped behaviour.
PLATFORM_ROOTS = ("robothor", "crm", "app/src", "infra", "helm", "templates")

#: Directories never worth reading -- build output and vendored dependencies.
SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".next",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "test-results",
    }
)

#: The vendor names, as case-insensitive regexes.
#:
#: ``apollo`` and ``impetus`` are bare substrings -- neither is a fragment of
#: any English word this codebase uses, so no narrowing is needed. ``freya``
#: carries a word boundary because the given name alone is short enough to
#: collide with an identifier; the two-word form is listed separately so the
#: full product name is caught either way.
BANNED_TERMS: tuple[tuple[str, str], ...] = (
    (r"apollo", "Apollo.io -- retired by operator decision 2026-08-21"),
    (r"princess\s+freya", "Princess Freya -- extracted to a plugin 2026-08-27"),
    (r"\bfreya\b", "Princess Freya -- extracted to a plugin 2026-08-27"),
    (r"impetus", "Impetus One -- an instance-land MCP adapter, not core"),
)

#: Files exempted from the grep gate. It is EMPTY and must stay empty: an
#: exemption here is a vendor back in core with paperwork. A legitimate hit
#: means the pattern is too broad -- narrow the pattern, do not list the file.
GATE_ALLOWLIST: frozenset[str] = frozenset()

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _platform_files() -> list[Path]:
    files: list[Path] = []
    for root in PLATFORM_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if SKIP_DIRS & set(path.relative_to(_REPO_ROOT).parts):
                continue
            files.append(path)
    return files


def _vendor_hits() -> list[str]:
    patterns = [(re.compile(term, re.IGNORECASE), why) for term, why in BANNED_TERMS]
    hits: list[str] = []
    for path in _platform_files():
        rel = str(path.relative_to(_REPO_ROOT))
        if rel in GATE_ALLOWLIST:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue  # binary asset -- no prose to leak
        for line_num, line in enumerate(content.splitlines(), 1):
            for pattern, why in patterns:
                if pattern.search(line):
                    hits.append(f"{rel}:{line_num} -- {why}\n      {line.strip()[:120]}")
                    break
    return hits


def test_no_instance_vendor_names_in_platform_code():
    """No platform root may name a retired or instance-only vendor.

    Not cosmetic. Each of these names left a different kind of residue: a
    dead comment header still describing a tool group that no longer exists,
    a benchmark allow-list hardcoding one adapter's tool names, an example
    URL pointing at one operator's SaaS tenant, fixture data naming a private
    repo. A fresh instance greps for the name, finds it in core, and
    reasonably concludes core supports the vendor.
    """
    hits = _vendor_hits()
    assert not hits, (
        "instance vendor names found in platform code -- core ships only what "
        "every instance needs (CLAUDE.md rule #1):\n  " + "\n  ".join(hits)
    )


def test_the_gate_allowlist_stays_empty():
    """An exemption is a vendor back in core with paperwork."""
    assert frozenset() == GATE_ALLOWLIST, (
        "GATE_ALLOWLIST must stay empty. A legitimate hit means the pattern "
        "is too broad -- narrow the pattern in BANNED_TERMS and say why in "
        "its comment, rather than exempting a file."
    )
