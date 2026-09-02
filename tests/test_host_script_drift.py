"""Tests for scripts/guardrail_watch.py's host-ops-script drift check.

The daily soak report already catches drift between the live engine systemd
drop-in and its repo mirror (check_dropin_drift() / check_dropin_drift.sh).
The three host ops scripts hand-copied to /usr/local/bin had NO equivalent —
which is how a permission fix sat in the repo for a month while the stale
installed copy kept failing. This extends the same drift check to those
three pairs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)


def test_default_pairs_cover_the_installed_host_scripts():
    pairs = dict(gw.HOST_SCRIPT_DRIFT_PAIRS)
    assert pairs["/usr/local/bin/robothor-wal-archive.sh"] == "scripts/wal-archive.sh"
    assert pairs["/usr/local/bin/robothor-thermal-guard.sh"] == "scripts/thermal-guard.sh"


def test_no_pair_for_a_mirror_the_installer_no_longer_writes():
    """A drift pair for a file nothing installs reports "missing" forever, and
    a permanently red check is one the operator stops reading. pg-basebackup.sh
    and wal-offsite.sh are run from the workspace by their units and source a
    sibling /usr/local/bin does not have."""
    pairs = dict(gw.HOST_SCRIPT_DRIFT_PAIRS)
    assert "/usr/local/bin/robothor-pg-basebackup.sh" not in pairs
    assert "/usr/local/bin/robothor-wal-offsite.sh" not in pairs


def test_reports_ok_when_live_matches_mirror(tmp_path, capsys):
    live = tmp_path / "live.sh"
    mirror = tmp_path / "mirror.sh"
    live.write_text("#!/usr/bin/env bash\necho hi\n")
    mirror.write_text("#!/usr/bin/env bash\necho hi\n")

    gw.check_host_script_drift(pairs=[(str(live), str(mirror))])

    out = capsys.readouterr().out
    assert "host ops script drift check" in out
    assert "OK" in out


def test_reports_drift_when_live_diverges_from_mirror(tmp_path, capsys):
    live = tmp_path / "live.sh"
    mirror = tmp_path / "mirror.sh"
    live.write_text("#!/usr/bin/env bash\necho hi\necho THE FIX\n")
    mirror.write_text("#!/usr/bin/env bash\necho hi\n")

    gw.check_host_script_drift(pairs=[(str(live), str(mirror))])

    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert str(live) in out


def test_reports_missing_when_live_file_absent(tmp_path, capsys):
    live = tmp_path / "nope.sh"
    mirror = tmp_path / "mirror.sh"
    mirror.write_text("#!/usr/bin/env bash\necho hi\n")

    gw.check_host_script_drift(pairs=[(str(live), str(mirror))])

    out = capsys.readouterr().out
    assert "missing" in out.lower()


def test_checks_every_pair_given(tmp_path, capsys):
    live1 = tmp_path / "live1.sh"
    mirror1 = tmp_path / "mirror1.sh"
    live1.write_text("a")
    mirror1.write_text("a")

    live2 = tmp_path / "live2.sh"
    mirror2 = tmp_path / "mirror2.sh"
    live2.write_text("b")
    mirror2.write_text("different")

    gw.check_host_script_drift(pairs=[(str(live1), str(mirror1)), (str(live2), str(mirror2))])

    out = capsys.readouterr().out
    assert "OK" in out
    assert "DRIFT" in out
