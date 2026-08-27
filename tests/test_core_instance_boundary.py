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

from pathlib import Path

HANDLERS = Path(__file__).resolve().parents[1] / "robothor" / "engine" / "tools" / "handlers"

#: Integrations that are one operator's, not every operator's. Each is a
#: known boundary violation awaiting extraction to a plugin.
GRANDFATHERED: set[str] = {
    "jira.py",       # COMMON-BUT-PLUGGABLE: one vendor behind no interface
    "github_api.py",  # COMMON-BUT-PLUGGABLE: same
    "pf.py",          # INSTANCE-SPECIFIC: vessel sensors
}

#: Vendors that must not appear in core at all. Apollo was retired by
#: operator decision on 2026-08-21 ("I'm done with Apollo"), not by repair.
BANNED_VENDORS = ("apollo",)


def _handler_files() -> set[str]:
    return {p.name for p in HANDLERS.glob("*.py") if p.name != "__init__.py"}


def test_no_banned_vendor_survives_in_core():
    present = _handler_files()
    offenders = [
        name for name in present
        if any(v in name.lower() for v in BANNED_VENDORS)
    ]
    assert not offenders, (
        f"a retired vendor integration is still in core: {offenders}. It was "
        "removed by decision, not by repair -- leaving it means agents are "
        "still offered a tool that only returns errors."
    )


def test_banned_vendors_are_not_dispatched():
    """Deleting the file is not enough if dispatch still imports it."""
    dispatch = (HANDLERS.parent / "dispatch.py").read_text().lower()
    for vendor in BANNED_VENDORS:
        assert vendor not in dispatch, (
            f"dispatch.py still wires {vendor!r} into the tool surface"
        )


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
