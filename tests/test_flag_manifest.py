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


def test_manifest_entries_have_required_fields():
    data = yaml.safe_load(MANIFEST.read_text())
    for entry in data["flags"]:
        for field in ("name", "owner", "mode", "soak"):
            assert field in entry, f"{entry.get('name', entry)} missing {field!r}"
        assert entry["mode"] in ("off", "observe", "alert", "enforce", "on"), entry["name"]
        # every non-terminal flag must carry a promotion deadline
        if entry["mode"] in ("observe", "alert"):
            assert entry.get("planned_promotion"), (
                f"{entry['name']} is in {entry['mode']} but has no planned_promotion date"
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
