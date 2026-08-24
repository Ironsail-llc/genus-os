"""guardrail_watch runs the instance manifest validation daily.

The strict validator (scripts/validate_agents.py) validated only git-TRACKED
manifests, and every real instance manifest is gitignored — so a box running
25 manifests had exactly one validated, in CI, where instance defects can
never appear. Its first --instance run found two live fleet defects (a
schema-required field missing on curator; email-briefing declaring a
status_file with no write tools). Nothing was scheduled to ever look.

This wires --instance into the daily guardrail watch as a DB-free check. A
FAILING fleet exits the run non-zero, so the existing OnFailure= pager fires —
the same delivery path every other daily check uses.
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

GOOD = """\
id: probe-good
name: Probe
description: test fixture
version: "2026-08-24"
department: system
model:
  primary: openrouter/xiaomi/mimo-v2.5
schedule:
  cron: ""
delivery:
  mode: none
tools_allowed: []
instructions: test
"""

BROKEN = """\
id: probe-broken
model:
  fallbacks:
    - a
  - b
"""


def test_clean_fleet_passes(tmp_path: Path, capsys):
    (tmp_path / "probe-good.yaml").write_text(GOOD)
    ok = gw.check_instance_manifests(manifest_dir=tmp_path, workspace=REPO_ROOT)
    out = capsys.readouterr().out
    assert ok, out
    assert "instance manifest validation" in out


def test_unparseable_manifest_fails_the_check(tmp_path: Path, capsys):
    """The 2026-08-23 defect class must fail the daily run, not pass it."""
    (tmp_path / "probe-good.yaml").write_text(GOOD)
    (tmp_path / "probe-broken.yaml").write_text(BROKEN)
    ok = gw.check_instance_manifests(manifest_dir=tmp_path, workspace=REPO_ROOT)
    out = capsys.readouterr().out
    assert not ok
    assert "probe-broken.yaml" in out


def test_schema_failure_fails_the_check(tmp_path: Path, capsys):
    (tmp_path / "no-model.yaml").write_text(
        "id: probe-bad\nname: X\ndescription: d\nversion: '1'\n"
        "schedule:\n  cron: ''\ndelivery:\n  mode: none\ntools_allowed: []\n"
    )
    ok = gw.check_instance_manifests(manifest_dir=tmp_path, workspace=REPO_ROOT)
    out = capsys.readouterr().out
    assert not ok, out


def test_validator_crash_is_a_failure_not_a_pass(tmp_path: Path, capsys):
    """A watchdog whose probe dies must not report health. Point it at a
    validator that cannot run."""
    (tmp_path / "probe-good.yaml").write_text(GOOD)
    ok = gw.check_instance_manifests(
        manifest_dir=tmp_path, workspace=REPO_ROOT, script=tmp_path / "does-not-exist.py"
    )
    out = capsys.readouterr().out
    assert not ok
    assert "could not run" in out.lower()


def test_main_exits_nonzero_when_manifests_fail(tmp_path: Path, monkeypatch):
    """The finding must reach the pager, and OnFailure only fires on rc != 0."""
    (tmp_path / "probe-broken.yaml").write_text(BROKEN)
    monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
    monkeypatch.setattr(gw, "check_host_script_drift", lambda: None)
    monkeypatch.setattr(gw, "_run_db_dependent_checks", lambda: None)
    real_check = gw.check_instance_manifests
    monkeypatch.setattr(
        gw,
        "check_instance_manifests",
        lambda **kw: real_check(manifest_dir=tmp_path, workspace=REPO_ROOT),
    )
    assert gw.main() == 1
