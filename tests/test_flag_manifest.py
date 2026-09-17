"""Tests for the flag manifest (infra/flags.yaml) and the soak-deadline nag
logic in scripts/guardrail_watch.py.

The manifest is the single inventory of guardrail/RIP flags: owner, current
production mode, planned promotion date, and soak criteria. guardrail_watch
nags (stdout section + Telegram when configured) for any flag sitting in a
pre-enforce mode past its planned promotion date — the systemic fix for
"48h soak" silently becoming 44 days.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "infra" / "flags.yaml"

spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
guardrail_watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spec and guardrail_watch)


def test_manifest_exists_and_parses():
    data = yaml.safe_load(MANIFEST.read_text())
    assert isinstance(data.get("flags"), list) and data["flags"], "flags list empty"


#: Marker a soak note uses to state what is actually blocking a promotion.
#: A date pushed out with no reason is indistinguishable from a date nobody
#: read, which is how "48h soak" became 44 days.
BLOCKER_MARKER = "BLOCKER:"

#: The one legitimate way an entry may sit in a pre-enforce mode with no
#: promotion date: the promotion is not applicable on this instance, and that
#: is a recorded decision rather than a missing one.
PROMOTION_NA = "n/a-on-this-instance"


def blocker_lines(entry: dict) -> list[str]:
    """The non-empty ``BLOCKER:`` lines in an entry's soak note."""
    return [
        line.strip()
        for line in str(entry.get("soak", "")).splitlines()
        if line.strip().startswith(BLOCKER_MARKER)
        and line.strip().removeprefix(BLOCKER_MARKER).strip()
    ]


def test_manifest_entries_have_required_fields():
    data = yaml.safe_load(MANIFEST.read_text())
    for entry in data["flags"]:
        for field in ("name", "owner", "mode", "soak"):
            assert field in entry, f"{entry.get('name', entry)} missing {field!r}"
        if entry.get("values"):
            # A value-set flag is a setting, not a rollout ladder: its `mode` is
            # the posture production runs, spelled in its OWN values.
            assert entry["mode"] in entry["values"], (
                f"{entry['name']}: mode {entry['mode']!r} is not one of its "
                f"declared values {entry['values']}"
            )
        else:
            assert entry["mode"] in ("off", "observe", "alert", "enforce", "on"), entry["name"]
        # every non-terminal flag must carry a promotion deadline, or say in
        # `promotion:` why it will never have one
        if entry["mode"] in ("observe", "alert"):
            assert entry.get("planned_promotion") or entry.get("promotion") == PROMOTION_NA, (
                f"{entry['name']} is in {entry['mode']} but has neither a "
                f"planned_promotion date nor promotion: {PROMOTION_NA}"
            )
        # ... and never both: two answers to one question is no answer.
        assert not (entry.get("planned_promotion") and entry.get("promotion")), (
            f"{entry['name']} carries both planned_promotion and promotion"
        )


def test_blocker_lines_reads_only_non_empty_markers():
    assert blocker_lines({"soak": "text\nBLOCKER: the probe has never run\nmore"}) == [
        "BLOCKER: the probe has never run"
    ]
    assert blocker_lines({"soak": "no marker here"}) == []
    assert blocker_lines({"soak": "BLOCKER:"}) == []
    assert blocker_lines({}) == []


def test_dated_entries_name_what_is_blocking_them():
    """Re-dating a missed promotion without writing down why is how a deadline
    becomes wallpaper. Any entry in observe/alert that carries a
    planned_promotion must say, in its soak note, what has to happen first.

    This used to apply only to entries already past their date, which made the
    test a calendar bomb: main turned red on 2026-09-10 and again on 2026-09-11
    with no code change, once per flag as each date passed. The daily
    ``guardrail_watch`` nag owns "overdue"; this test owns "stated". In a
    folded ``>`` soak note the marker must start a paragraph (blank line
    before it) or YAML folds it into the previous sentence."""
    data = yaml.safe_load(MANIFEST.read_text())
    silent = []
    for entry in data["flags"]:
        if entry.get("mode") not in ("observe", "alert"):
            continue
        if not entry.get("planned_promotion"):
            continue
        if not blocker_lines(entry):
            silent.append(entry["name"])
    assert not silent, (
        f"dated promotion with no stated blocker: {silent}. Add a "
        f"'{BLOCKER_MARKER} ...' paragraph to soak: naming what must happen "
        "before the flip."
    )


def test_manifest_covers_live_dropin_mode_flags():
    """Every *_MODE flag in the versioned drop-in must appear in the manifest."""
    dropin = (
        REPO_ROOT / "infra" / "systemd" / "robothor-engine.service.d" / "upgrade-rip-flags.conf"
    ).read_text()
    manifest_names = {e["name"] for e in yaml.safe_load(MANIFEST.read_text())["flags"]}
    for line in dropin.splitlines():
        line = line.strip()
        if line.startswith("Environment=") and "_MODE=" in line:
            flag = line.removeprefix("Environment=").split("=", 1)[0]
            assert flag in manifest_names, f"drop-in flag {flag} missing from infra/flags.yaml"


def test_overdue_flags_detected():
    flags = [
        {
            "name": "A_MODE",
            "mode": "observe",
            "planned_promotion": "2026-01-01",
            "owner": "x",
            "soak": "s",
        },
        {"name": "B_MODE", "mode": "enforce", "owner": "x", "soak": "s"},
        {
            "name": "C_MODE",
            "mode": "alert",
            "planned_promotion": "2099-01-01",
            "owner": "x",
            "soak": "s",
        },
    ]
    overdue = guardrail_watch.overdue_flags(flags, today=dt.date(2026, 7, 13))
    assert [f["name"] for f in overdue] == ["A_MODE"]


def test_overdue_nag_message_names_flag_and_days():
    flags = [
        {
            "name": "A_MODE",
            "mode": "observe",
            "planned_promotion": "2026-07-01",
            "owner": "ops",
            "soak": "zero events 48h",
        },
    ]
    msg = guardrail_watch.format_nag(
        guardrail_watch.overdue_flags(flags, today=dt.date(2026, 7, 13)),
        today=dt.date(2026, 7, 13),
    )
    assert "A_MODE" in msg
    assert "12" in msg  # days overdue
    assert "observe" in msg


def test_no_nag_when_nothing_overdue():
    msg = guardrail_watch.format_nag([], today=dt.date(2026, 7, 13))
    assert msg == ""


def test_manifest_modes_match_dropin_mirror():
    """For every *_MODE flag present in both the drop-in mirror and the
    manifest, the recorded modes must agree — a flip PR must update both."""
    dropin = (
        REPO_ROOT / "infra" / "systemd" / "robothor-engine.service.d" / "upgrade-rip-flags.conf"
    ).read_text()
    manifest_modes = {e["name"]: e["mode"] for e in yaml.safe_load(MANIFEST.read_text())["flags"]}
    checked = 0
    for line in dropin.splitlines():
        line = line.strip()
        if line.startswith("Environment=") and "_MODE=" in line:
            flag, value = line.removeprefix("Environment=").split("=", 1)
            assert manifest_modes.get(flag) == value, (
                f"{flag}: drop-in mirror says {value!r}, manifest says "
                f"{manifest_modes.get(flag)!r} — update both in the flip PR"
            )
            checked += 1
    assert checked >= 5, "expected several *_MODE flags in the drop-in"


def dropin_environment() -> dict[str, str]:
    """Every ``Environment=NAME=VALUE`` in the versioned drop-in mirror."""
    text = (
        REPO_ROOT / "infra" / "systemd" / "robothor-engine.service.d" / "upgrade-rip-flags.conf"
    ).read_text()
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        name, sep, value = line.removeprefix("Environment=").partition("=")
        if sep:
            out[name.strip()] = value.strip()
    return out


def test_enforced_flags_are_pinned_in_the_versioned_dropin():
    """A flag the manifest records at ``enforce`` must be SET in the drop-in.

    The manifest records intent and sets nothing at runtime, so a posture that
    exists nowhere versioned is governed by whatever unversioned layer happens
    to carry it — ``/etc/robothor/robothor.env``, which systemd applies AFTER
    the drop-in. A flip applied to the drop-in then does nothing, a rebuilt box
    comes up without the control, and ``flag_audit.py`` tags it
    ``SHADOW-LAYER:envfile`` every morning. That is exactly what
    ``ROBOTHOR_PER_USER_SESSIONS`` did from the day it shipped until 2026-09-17.

    A matching code default is not a substitute: it is the platform's opinion
    about a fresh install, not this instance's recorded posture, and a later
    refactor can move it without touching the manifest.
    """
    data = yaml.safe_load(MANIFEST.read_text())
    dropin = dropin_environment()
    enforced = [e["name"] for e in data["flags"] if str(e["mode"]) == "enforce"]
    missing = [name for name in enforced if name not in dropin]
    assert not missing, (
        f"manifest says enforce but the drop-in sets nothing: {missing}. Add "
        "Environment=<FLAG>=enforce to "
        "infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf"
    )
    disagree = {name: dropin[name] for name in enforced if dropin[name] != "enforce"}
    assert not disagree, f"drop-in value != the manifest's enforce: {disagree}"


def test_value_set_flags_declare_the_values_the_engine_accepts():
    """A flag whose values are a setting's options, not a rollout ladder,
    declares them with ``values:`` — and they must be exactly what
    ``robothor.flags.store.valid_values_for`` returns.

    Both halves matter. Without the declaration, every reader of the manifest
    (``flag_audit.py`` above all) has to guess at the four-rung ladder, and
    ``ROBOTHOR_CALENDAR_SEND_UPDATES`` — correctly sitting on its `all` default
    — read as `observe` and MISMATCHed forever. Without the mirror, the manifest
    could offer an operator a value the Controls API would refuse with a 422.
    """
    from robothor.flags.store import VALUE_SET_FLAGS, valid_values_for

    data = yaml.safe_load(MANIFEST.read_text())
    declared = {e["name"]: list(e["values"]) for e in data["flags"] if e.get("values")}
    assert declared, "no value-set flag declared in the manifest"
    for name, values in declared.items():
        assert values == list(valid_values_for(name)), (
            f"{name}: manifest values {values} != store.valid_values_for "
            f"{list(valid_values_for(name))}"
        )
    assert set(declared) == set(VALUE_SET_FLAGS), (
        "every value-set flag the store knows about must say so in the "
        f"manifest: store={sorted(VALUE_SET_FLAGS)} manifest={sorted(declared)}"
    )
