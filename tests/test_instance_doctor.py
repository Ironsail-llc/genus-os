"""Tests for scripts/instance_doctor.sh — the install-truth check.

scripts/install-units.sh reports installed/updated/unchanged. That is only one
direction: it can say what the repo put on the box, never what is on the box
that the repo did not put there. Everything in the list below was live on the
first Genus OS instance, and no command anywhere would print any of it:

  * a timer SYMLINKED into the repo checkout, so it was never rendered and a
    repo checkout move would have silently unscheduled it;
  * nine live robothor-* units with no template at all (two of them active) —
    a rebuilt box loses them;
  * twelve ``*.bak-*`` files sitting in robothor-engine.service.d/, which
    systemd ignores (it reads ``*.conf`` only) but a human reading the
    directory does not;
  * hand-written drop-ins with no repo mirror;
  * a unit enabled but not running, and one running but not enabled;
  * host ops scripts drifted from their repo copies;
  * flags set in BOTH /etc/robothor/robothor.env and a drop-in's
    ``Environment=``, where the env file silently wins.

The doctor is read-only and exits 1 on any finding, so a timer can page.
It reuses scripts/render-unit.sh (via scripts/check_dropin_drift.sh) rather
than re-implementing template comparison.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCTOR = REPO_ROOT / "scripts" / "instance_doctor.sh"
INSTALL_UNITS = REPO_ROOT / "scripts" / "install-units.sh"
INSTALL_HOST = REPO_ROOT / "scripts" / "install-host-scripts.sh"

WS = "/srv/genus"
USER = "alice"
HOME = "/home/alice"


def base_env(**overrides: str | None) -> dict[str, str]:
    """Subprocess env with every inherited ROBOTHOR_* stripped, so a real
    /etc/robothor/robothor.env or a developer's shell can never decide a
    test's outcome."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "ROBOTHOR_WORKSPACE": WS,
            "ROBOTHOR_SERVICE_USER": USER,
            "ROBOTHOR_SERVICE_HOME": HOME,
            "ROBOTHOR_ENV_FILE": "/nonexistent/robothor.env",
        }
    )
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def run_doctor(root: Path, env: dict[str, str] | None = None, *extra: str):
    return subprocess.run(
        ["bash", str(DOCTOR), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        timeout=180,
        env=env or base_env(),
    )


@pytest.fixture
def installed_root(tmp_path: Path) -> Path:
    """A filesystem root the repo's own installers just populated.

    This is the definition of "in sync": everything the repo installs, freshly
    installed. The doctor must report zero findings here, or every real finding
    it reports later is noise.
    """
    root = tmp_path / "root"
    env = base_env()
    for installer in (INSTALL_UNITS, INSTALL_HOST):
        result = subprocess.run(
            ["bash", str(installer), "--root", str(root)],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    return root


def system_dir(root: Path) -> Path:
    return root / "etc" / "systemd" / "system"


def fake_systemctl(tmp_path: Path, states: dict[str, str]) -> Path:
    """A PATH-free systemctl stand-in driven by a key=value table.

    Keys are ``<verb-or-property>:<unit>``, e.g. ``enabled:robothor-x.service``,
    ``active:robothor-x.service``, ``Type:robothor-x.service``,
    ``TriggeredBy:robothor-x.service``. Absent keys answer empty, which the
    doctor must treat as "unknown", never as "healthy".
    """
    table = tmp_path / "systemctl-state"
    table.write_text("".join(f"{k}={v}\n" for k, v in states.items()))
    stub = tmp_path / "fake-systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'STATE="{table}"\n'
        'case "$1" in\n'
        '  is-enabled) key="enabled:$2" ;;\n'
        '  is-active)  key="active:$2" ;;\n'
        '  show)       key="$3:$5" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
        'line="$(grep -m1 "^${key}=" "$STATE" || true)"\n'
        'printf "%s\\n" "${line#*=}"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


# ── the contract ─────────────────────────────────────────────────────────────


def test_doctor_exists_and_is_executable():
    assert DOCTOR.exists(), "scripts/instance_doctor.sh missing"
    assert DOCTOR.stat().st_mode & 0o111, "instance_doctor.sh is not executable"


def test_freshly_installed_root_is_clean(installed_root: Path):
    result = run_doctor(installed_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FINDING" not in result.stdout, result.stdout


def test_four_seeded_findings_all_reported(installed_root: Path, tmp_path: Path):
    sysd = system_dir(installed_root)

    # 1. an inert .bak file in a drop-in directory (systemd reads *.conf only)
    dropin_dir = sysd / "robothor-engine.service.d"
    (dropin_dir / "upgrade-rip-flags.conf.bak-20260713").write_text("[Service]\n")

    # 2. a unit symlinked into the repo checkout instead of rendered
    symlinked = sysd / "robothor-liveness.timer"
    symlinked.unlink()
    symlinked.symlink_to(REPO_ROOT / "infra" / "systemd" / "robothor-liveness.timer")

    # 3. a live unit with no template in infra/systemd/
    (sysd / "robothor-orphan.service").write_text("[Service]\nExecStart=/bin/true\n")

    # 4. a hand-written drop-in with no repo mirror
    (dropin_dir / "hand-edit.conf").write_text("[Service]\nEnvironment=X=1\n")

    result = run_doctor(installed_root)

    assert result.returncode == 1, result.stdout + result.stderr
    out = result.stdout
    assert "upgrade-rip-flags.conf.bak-20260713" in out, out
    assert "robothor-liveness.timer" in out, out
    assert "robothor-orphan.service" in out, out
    assert "hand-edit.conf" in out, out
    assert out.count("FINDING") >= 4, out


def test_hand_edited_installed_unit_is_reported_as_drift(installed_root: Path):
    target = system_dir(installed_root) / "robothor-engine.service"
    target.write_text(target.read_text() + "\n# unversioned live edit\n")

    result = run_doctor(installed_root)

    assert result.returncode == 1, result.stdout
    assert "robothor-engine.service" in result.stdout
    assert "unversioned live edit" in result.stdout, "the diff itself must be shown"


def test_masked_unit_is_not_a_finding(installed_root: Path):
    """A unit symlinked to /dev/null is MASKED — a deliberate act, not drift.
    Reporting it would train the operator to ignore the symlink finding."""
    sysd = system_dir(installed_root)
    target = sysd / "robothor-vnc.service"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to("/dev/null")

    result = run_doctor(installed_root)

    assert result.returncode == 0, result.stdout
    assert "robothor-vnc.service" not in result.stdout


def test_untemplated_unit_reports_enabled_and_active_state(
    installed_root: Path, tmp_path: Path
):
    """Whether a template-less unit is RUNNING decides how urgent it is."""
    sysd = system_dir(installed_root)
    (sysd / "robothor-orphan.service").write_text("[Service]\nExecStart=/bin/true\n")
    stub = fake_systemctl(
        tmp_path,
        {
            "enabled:robothor-orphan.service": "enabled",
            "active:robothor-orphan.service": "active",
        },
    )

    result = run_doctor(installed_root, base_env(ROBOTHOR_SYSTEMCTL=str(stub)))

    assert result.returncode == 1, result.stdout
    line = next(
        line for line in result.stdout.splitlines() if "robothor-orphan.service" in line
    )
    assert "enabled=enabled" in line, line
    assert "active=active" in line, line


def test_allow_file_suppresses_a_known_untemplated_unit(
    installed_root: Path, tmp_path: Path
):
    sysd = system_dir(installed_root)
    (sysd / "robothor-orphan.service").write_text("[Service]\nExecStart=/bin/true\n")
    allow = tmp_path / "instance-units.allow"
    allow.write_text("# instance-only units, deliberately not templated\nrobothor-orphan.service\n")

    result = run_doctor(installed_root, base_env(), "--allow-file", str(allow))

    assert result.returncode == 0, result.stdout
    assert "robothor-orphan.service" not in result.stdout.replace("allow", "")


def test_an_unrenderable_mirror_is_not_reported_as_drift(installed_root: Path):
    """check_dropin_drift.sh exits 2 for "I could not compare these" — a missing
    renderer, or a render env it cannot resolve — and 1 for "these differ".
    Collapsing 2 into template-drift sends the operator to reconcile a
    difference that was never measured, and hides the real fault: the doctor
    is not checking these units at all.
    """
    result = run_doctor(installed_root, base_env(ROBOTHOR_WORKSPACE=None))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot-compare" in result.stdout, result.stdout
    assert "template-drift" not in result.stdout, (
        "an uncomparable unit was reported as drifted\n" + result.stdout
    )


# ── the allow file ───────────────────────────────────────────────────────────


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_allow_file_is_reported(installed_root: Path, tmp_path: Path):
    """Silently ignoring it means every entry stops suppressing and the
    operator reads a page full of findings they had already triaged — with no
    line anywhere saying why."""
    sysd = system_dir(installed_root)
    (sysd / "robothor-orphan.service").write_text("[Service]\nExecStart=/bin/true\n")
    allow = tmp_path / "instance-units.allow"
    allow.write_text("robothor-orphan.service\n")
    allow.chmod(0o000)
    try:
        result = run_doctor(installed_root, base_env(), "--allow-file", str(allow))
    finally:
        allow.chmod(0o600)

    assert str(allow) in result.stderr, result.stderr
    assert "robothor-orphan.service" in result.stdout, result.stdout


def test_an_allow_entry_that_matched_nothing_is_reported(
    installed_root: Path, tmp_path: Path
):
    """A stale entry suppresses nothing and looks like coverage. The unit was
    removed, or it finally got a template — either way the operator should
    delete the line rather than carry it forever."""
    allow = tmp_path / "instance-units.allow"
    allow.write_text("robothor-long-gone.service\n")

    result = run_doctor(installed_root, base_env(), "--allow-file", str(allow))

    assert result.returncode == 0, result.stdout
    assert "robothor-long-gone.service" in result.stderr, result.stderr


def test_the_allow_file_cannot_suppress_drift_inert_files_or_symlinks(
    installed_root: Path, tmp_path: Path
):
    """The allow file says "this unit is deliberately instance-only", which is
    a statement about having no template. It is not a mute button: a live unit
    that no longer matches its own template, a file systemd ignores, and a unit
    that was symlinked instead of rendered are all still wrong for a unit
    someone deliberately listed here."""
    sysd = system_dir(installed_root)
    drifted = sysd / "robothor-engine.service"
    drifted.write_text(drifted.read_text() + "\n# unversioned live edit\n")
    inert = sysd / "robothor-engine.service.d" / "upgrade-rip-flags.conf.bak-20260713"
    inert.write_text("[Service]\n")
    symlinked = sysd / "robothor-liveness.timer"
    symlinked.unlink()
    symlinked.symlink_to(REPO_ROOT / "infra" / "systemd" / "robothor-liveness.timer")

    allow = tmp_path / "instance-units.allow"
    allow.write_text(
        "robothor-engine.service\n"
        "robothor-engine.service.d/upgrade-rip-flags.conf.bak-20260713\n"
        "robothor-liveness.timer\n"
    )

    result = run_doctor(installed_root, base_env(), "--allow-file", str(allow))

    assert result.returncode == 1, result.stdout
    assert "template-drift" in result.stdout, result.stdout
    assert "inert-file" in result.stdout, result.stdout
    assert "symlink" in result.stdout, result.stdout


def test_enabled_but_not_active_is_reported(installed_root: Path, tmp_path: Path):
    stub = fake_systemctl(
        tmp_path,
        {
            "enabled:robothor-vision.service": "enabled",
            "active:robothor-vision.service": "inactive",
            "Type:robothor-vision.service": "simple",
            "TriggeredBy:robothor-vision.service": "",
        },
    )
    sysd = system_dir(installed_root)
    shutil.copy(sysd / "robothor-engine.service", sysd / "robothor-vision.service")

    result = run_doctor(installed_root, base_env(ROBOTHOR_SYSTEMCTL=str(stub)))

    assert result.returncode == 1, result.stdout
    assert "robothor-vision.service" in result.stdout
    assert "enabled-not-active" in result.stdout, result.stdout


def test_active_but_disabled_is_reported(installed_root: Path, tmp_path: Path):
    """Running but not enabled: it disappears at the next reboot, silently."""
    stub = fake_systemctl(
        tmp_path,
        {
            "enabled:robothor-engine.service": "disabled",
            "active:robothor-engine.service": "active",
            "Type:robothor-engine.service": "simple",
            "TriggeredBy:robothor-engine.service": "",
        },
    )

    result = run_doctor(installed_root, base_env(ROBOTHOR_SYSTEMCTL=str(stub)))

    assert result.returncode == 1, result.stdout
    assert "active-not-enabled" in result.stdout, result.stdout


def test_timer_triggered_oneshot_is_not_reported(installed_root: Path, tmp_path: Path):
    """A oneshot service fired by a timer is inactive by design. Flagging it
    would bury the real findings under a dozen false ones."""
    stub = fake_systemctl(
        tmp_path,
        {
            "enabled:robothor-liveness.service": "enabled",
            "active:robothor-liveness.service": "inactive",
            "Type:robothor-liveness.service": "oneshot",
            "TriggeredBy:robothor-liveness.service": "robothor-liveness.timer",
        },
    )

    result = run_doctor(installed_root, base_env(ROBOTHOR_SYSTEMCTL=str(stub)))

    assert result.returncode == 0, result.stdout
    assert "instance-doctor" in result.stdout, "the doctor did not run at all"
    assert "robothor-liveness.service" not in result.stdout, result.stdout


def test_host_script_drift_is_reported(installed_root: Path):
    installed = installed_root / "usr" / "local" / "bin" / "robothor-thermal-guard.sh"
    installed.write_text(installed.read_text() + "\n# stale installed copy\n")

    result = run_doctor(installed_root)

    assert result.returncode == 1, result.stdout
    assert "robothor-thermal-guard.sh" in result.stdout
    assert "stale installed copy" in result.stdout


def test_env_file_shadowing_a_dropin_environment_is_reported(installed_root: Path):
    """systemd applies EnvironmentFile= AFTER the drop-in's Environment=, so a
    key set in both is governed by the env file — and a flag flipped in the
    versioned drop-in silently does nothing (2026-07-25)."""
    env_dir = installed_root / "etc" / "robothor"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "robothor.env").write_text("ROBOTHOR_RIP_7_ENABLED=false\n")
    dropin = (
        system_dir(installed_root)
        / "robothor-engine.service.d"
        / "upgrade-rip-flags.conf"
    )
    dropin.write_text(dropin.read_text() + "Environment=ROBOTHOR_RIP_7_ENABLED=true\n")

    result = run_doctor(installed_root)

    assert result.returncode == 1, result.stdout
    assert "ROBOTHOR_RIP_7_ENABLED" in result.stdout
    assert "env-shadow" in result.stdout, result.stdout


def test_doctor_is_read_only(installed_root: Path):
    """It runs on a live box from a timer. It must change nothing."""
    before = {
        p: p.stat().st_mtime_ns
        for p in sorted(installed_root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    result = run_doctor(installed_root)
    assert "instance-doctor" in result.stdout, "the doctor did not run at all"
    after = {
        p: p.stat().st_mtime_ns
        for p in sorted(installed_root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
    assert before == after, "instance_doctor.sh modified the filesystem"


def test_no_instance_data_in_the_script():
    """CLAUDE.md rule 1 — the doctor ships to every instance, so no operator
    home directory and no hardcoded workspace may appear in it. Naming the
    operator here, even to forbid the name, would itself be instance data."""
    text = DOCTOR.read_text()
    assert "/home/" not in text, "a home directory is instance data"
    assert "/opt/robothor" not in text, "the workspace comes from render-unit.sh, not a literal"


# ── guardrail-watch integration ──────────────────────────────────────────────
# A doctor nobody runs is a doctor that does not exist. It needs no database,
# so it belongs in guardrail_watch.main()'s DB-free block, which runs first and
# survives a postgres outage (tests/test_guardrail_watch_ordering.py).

_spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
assert _spec is not None and _spec.loader is not None
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)


def _fake_doctor(tmp_path: Path, exit_code: int, message: str = "seeded") -> Path:
    stub = tmp_path / "fake_instance_doctor.sh"
    stub.write_text(f"#!/usr/bin/env bash\necho '{message}'\nexit {exit_code}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def test_guardrail_watch_reports_doctor_findings(tmp_path: Path, capsys):
    ok = gw.check_instance_doctor(script=_fake_doctor(tmp_path, 1, "FINDING [symlink] x"))
    assert ok is False
    assert "FINDING [symlink] x" in capsys.readouterr().out


def test_guardrail_watch_passes_a_clean_doctor(tmp_path: Path):
    assert gw.check_instance_doctor(script=_fake_doctor(tmp_path, 0)) is True


def test_missing_doctor_is_not_health(tmp_path: Path, capsys):
    """A watchdog whose probe is gone must not report health."""
    ok = gw.check_instance_doctor(script=tmp_path / "does-not-exist.sh")
    assert ok is False
    assert "FAIL" in capsys.readouterr().out


def test_doctor_runs_in_the_db_free_block(monkeypatch, capsys):
    """It needs no database, so a postgres outage must not take it down —
    the 2026-08-16 failure mode this ordering exists to prevent."""
    monkeypatch.setattr(gw, "send_telegram", lambda text: False)
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
    monkeypatch.setattr(gw, "check_host_script_drift", lambda pairs=None: None)
    monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
    monkeypatch.setattr(
        gw, "check_instance_doctor", lambda script=None: print("SENTINEL-DOCTOR-RAN") or True
    )

    def _boom(autocommit: bool = False):
        raise RuntimeError("postgres is not up yet")

    monkeypatch.setattr("robothor.db.connection.get_connection", _boom)

    gw.main()

    out = capsys.readouterr().out
    assert "SENTINEL-DOCTOR-RAN" in out
    assert out.index("SENTINEL-DOCTOR-RAN") < out.index("DATABASE")


def test_doctor_findings_fail_the_watch(monkeypatch):
    """Findings must reach the operator: rc=1 fires the unit's OnFailure pager.
    The suppression path is the allow file, not a swallowed exit code."""
    monkeypatch.setattr(gw, "send_telegram", lambda text: False)
    monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
    monkeypatch.setattr(gw, "check_host_script_drift", lambda pairs=None: None)
    monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
    monkeypatch.setattr(gw, "_run_db_dependent_checks", lambda: None)
    monkeypatch.setattr(gw, "check_instance_doctor", lambda script=None: False)

    assert gw.main() == 1
