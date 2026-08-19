"""Tests for scripts/guardrail_watch.py's check_dropin_drift().

Originally this checked exactly one file (upgrade-rip-flags.conf), driven by
check_dropin_drift.sh's own hardcoded defaults. That meant hardening.conf
could drift 26 lines behind live and zz-sandbox.conf could exist ONLY on the
live host, with no repo mirror at all — and nothing would ever say so.

This generalizes the check to every *.conf file mirrored under
infra/systemd/robothor-engine.service.d/, following the same injectable-pairs
pattern as check_host_script_drift() (tests/test_host_script_drift.py) so the
discovery logic and the reporting logic are each independently testable.
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


# --- dropin_conf_pairs(): discovery -----------------------------------------


def test_discovers_every_conf_file_in_mirror_dir(tmp_path):
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    (mirror_dir / "a.conf").write_text("a")
    (mirror_dir / "b.conf").write_text("b")
    (mirror_dir / "notes.txt").write_text("ignore me")
    live_dir = Path("/etc/systemd/system/robothor-engine.service.d")

    pairs = gw.dropin_conf_pairs(mirror_dir=mirror_dir, live_dir=live_dir)

    assert pairs == [
        (str(live_dir / "a.conf"), str(mirror_dir / "a.conf")),
        (str(live_dir / "b.conf"), str(mirror_dir / "b.conf")),
    ]


def test_pairs_empty_when_mirror_dir_missing(tmp_path):
    pairs = gw.dropin_conf_pairs(
        mirror_dir=tmp_path / "does-not-exist",
        live_dir=Path("/etc/systemd/system/robothor-engine.service.d"),
    )
    assert pairs == []


def test_default_pairs_cover_the_repo_conf_mirrors():
    """Real repo state: hardening.conf and zz-sandbox.conf are both mirrored."""
    pairs = gw.dropin_conf_pairs()
    mirrors = {Path(mirror).name: live for live, mirror in pairs}
    assert mirrors["hardening.conf"] == "/etc/systemd/system/robothor-engine.service.d/hardening.conf"
    assert mirrors["zz-sandbox.conf"] == "/etc/systemd/system/robothor-engine.service.d/zz-sandbox.conf"
    assert (
        mirrors["upgrade-rip-flags.conf"]
        == "/etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf"
    )


# --- check_dropin_drift(): reporting ----------------------------------------


def test_reports_ok_when_live_matches_mirror(tmp_path, capsys):
    live = tmp_path / "hardening.conf"
    mirror = tmp_path / "mirror.conf"
    live.write_text("NoNewPrivileges=yes\n")
    mirror.write_text("NoNewPrivileges=yes\n")

    gw.check_dropin_drift(pairs=[(str(live), str(mirror))])

    out = capsys.readouterr().out
    assert "drop-in drift check" in out
    assert "OK" in out


def test_reports_drift_when_live_diverges_from_mirror(tmp_path, capsys):
    live = tmp_path / "zz-sandbox.conf"
    mirror = tmp_path / "mirror.conf"
    live.write_text("ProtectHostname=no\nReadWritePaths=/mnt/robothor-backup\n")
    mirror.write_text("ProtectHostname=no\n")

    gw.check_dropin_drift(pairs=[(str(live), str(mirror))])

    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert str(live) in out


def test_reports_missing_when_live_file_absent(tmp_path, capsys):
    live = tmp_path / "nope.conf"
    mirror = tmp_path / "mirror.conf"
    mirror.write_text("NoNewPrivileges=yes\n")

    gw.check_dropin_drift(pairs=[(str(live), str(mirror))])

    out = capsys.readouterr().out
    assert "missing" in out.lower()


def test_iterates_every_pair_given(tmp_path, capsys):
    live1 = tmp_path / "live1.conf"
    mirror1 = tmp_path / "mirror1.conf"
    live1.write_text("a")
    mirror1.write_text("a")

    live2 = tmp_path / "live2.conf"
    mirror2 = tmp_path / "mirror2.conf"
    live2.write_text("b")
    mirror2.write_text("different")

    gw.check_dropin_drift(pairs=[(str(live1), str(mirror1)), (str(live2), str(mirror2))])

    out = capsys.readouterr().out
    assert "OK" in out
    assert "DRIFT" in out
