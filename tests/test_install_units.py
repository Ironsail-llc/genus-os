"""Tests for scripts/render-unit.sh + scripts/install-units.sh — rendering the
systemd unit templates in infra/systemd/ into installable units.

The repo templates carry placeholders systemd cannot expand: `${ROBOTHOR_WORKSPACE}`
in ExecStart lines (`systemd-analyze verify` fails on it), `/opt/robothor` paths,
`User=robothor`/`Group=robothor` accounts, and `%h` — which expands to /root in
system units, a documented past incident. Installed copies were hand-edited, so
fixes in the repo never reached the box (the same failure mode
scripts/install-host-scripts.sh closed for the host ops scripts).

The renderer substitutes the canonical placeholder spellings:
  /opt/robothor            -> $ROBOTHOR_WORKSPACE
  ${ROBOTHOR_WORKSPACE}    -> $ROBOTHOR_WORKSPACE   (legacy spelling)
  User=robothor            -> User=$ROBOTHOR_SERVICE_USER   (exact line only)
  Group=robothor           -> Group=$ROBOTHOR_SERVICE_USER  (exact line only)
  /home/robothor           -> service user's home
  %h                       -> service user's home   (legacy spelling)

and fails loudly when a variable is unresolvable or a placeholder survives
rendering. The installer renders every robothor-* unit (delphi-* units are
instance-land and deliberately not installed), gates .service files on
systemd-analyze verify (skipped under --root test mode), and installs
idempotently to <root>/etc/systemd/system/.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER = REPO_ROOT / "scripts" / "render-unit.sh"
INSTALL = REPO_ROOT / "scripts" / "install-units.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

WS = "/srv/genus"
USER = "alice"
HOME = "/home/alice"


def base_env(**overrides: str | None) -> dict[str, str]:
    """Subprocess env with all ROBOTHOR_* inherited vars stripped, then the
    test's own values applied. ROBOTHOR_ENV_FILE points at a path that does
    not exist so a real /etc/robothor/robothor.env on the dev box can never
    leak into a test."""
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


def render(src: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RENDER), str(src)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def run_install(root: Path, env: dict[str, str], *extra: str):
    return subprocess.run(
        ["bash", str(INSTALL), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.fixture()
def sample_unit(tmp_path: Path) -> Path:
    unit = tmp_path / "robothor-sample.service"
    unit.write_text(
        "[Unit]\n"
        "Description=Sample\n"
        "\n"
        "[Service]\n"
        "User=robothor\n"
        "Group=robothor\n"
        "WorkingDirectory=/opt/robothor\n"
        "ExecStart=/opt/robothor/venv/bin/python -m robothor.engine.daemon\n"
        "ReadWritePaths=/home/robothor/.config/robothor\n"
        "Environment=NOT_A_USER=robothor\n"
    )
    return unit


# ── Existence ────────────────────────────────────────────────────────────────


def test_scripts_exist_and_are_executable():
    for script in (RENDER, INSTALL):
        assert script.exists(), f"{script} missing"
        assert script.stat().st_mode & 0o111, f"{script} is not executable"


# ── Renderer: substitutions ──────────────────────────────────────────────────


def test_renders_opt_robothor_to_workspace(sample_unit: Path):
    result = render(sample_unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WorkingDirectory={WS}\n" in result.stdout
    assert f"ExecStart={WS}/venv/bin/python -m robothor.engine.daemon\n" in result.stdout
    assert "/opt/robothor" not in result.stdout


def test_renders_legacy_workspace_var_spelling(tmp_path: Path):
    unit = tmp_path / "robothor-legacy.service"
    unit.write_text("[Service]\nUser=robothor\nExecStart=${ROBOTHOR_WORKSPACE}/scripts/run.sh\n")
    result = render(unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ExecStart={WS}/scripts/run.sh\n" in result.stdout
    assert "${ROBOTHOR_WORKSPACE}" not in result.stdout


def test_renders_user_and_group(sample_unit: Path):
    result = render(sample_unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"User={USER}\n" in result.stdout
    assert f"Group={USER}\n" in result.stdout


def test_user_substitution_is_line_anchored(sample_unit: Path):
    """Only exact `User=robothor` / `Group=robothor` lines are the placeholder;
    `robothor` appearing in other values must survive untouched."""
    result = render(sample_unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Environment=NOT_A_USER=robothor\n" in result.stdout


def test_other_service_accounts_are_untouched(tmp_path: Path):
    unit = tmp_path / "robothor-pg.service"
    unit.write_text("[Service]\nUser=postgres\nExecStart=/opt/robothor/scripts/x.sh\n")
    result = render(unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "User=postgres\n" in result.stdout


def test_renders_home_placeholder(sample_unit: Path):
    result = render(sample_unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ReadWritePaths={HOME}/.config/robothor\n" in result.stdout
    assert "/home/robothor" not in result.stdout


def test_renders_legacy_percent_h(tmp_path: Path):
    unit = tmp_path / "robothor-h.service"
    unit.write_text("[Service]\nUser=robothor\nReadWritePaths=%h/.config/gws\n")
    result = render(unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ReadWritePaths={HOME}/.config/gws\n" in result.stdout
    assert "%h" not in result.stdout


def test_percent_i_instance_specifier_is_preserved(tmp_path: Path):
    unit = tmp_path / "robothor-a@.service"
    unit.write_text("[Service]\nUser=robothor\nExecStart=/opt/robothor/scripts/alert.sh %i\n")
    result = render(unit, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "%i" in result.stdout


def test_home_is_derived_from_passwd_when_unset(tmp_path: Path):
    import pwd

    me = pwd.getpwuid(os.getuid())
    unit = tmp_path / "robothor-home.service"
    unit.write_text("[Service]\nUser=robothor\nReadWritePaths=%h/.config/gog\n")
    env = base_env(ROBOTHOR_SERVICE_HOME=None, ROBOTHOR_SERVICE_USER=me.pw_name)
    result = render(unit, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ReadWritePaths={me.pw_dir}/.config/gog\n" in result.stdout


# ── Renderer: failure modes ──────────────────────────────────────────────────


def test_fails_without_service_user(sample_unit: Path):
    result = render(sample_unit, base_env(ROBOTHOR_SERVICE_USER=None))
    assert result.returncode != 0
    assert "ROBOTHOR_SERVICE_USER" in result.stderr


def test_fails_without_workspace(sample_unit: Path):
    result = render(sample_unit, base_env(ROBOTHOR_WORKSPACE=None))
    assert result.returncode != 0
    assert "ROBOTHOR_WORKSPACE" in result.stderr


def test_fails_when_home_needed_but_unresolvable(tmp_path: Path):
    unit = tmp_path / "robothor-h.service"
    unit.write_text("[Service]\nUser=robothor\nReadWritePaths=%h/.config/gws\n")
    env = base_env(
        ROBOTHOR_SERVICE_HOME=None,
        ROBOTHOR_SERVICE_USER="no-such-user-zz9",
    )
    result = render(unit, env)
    assert result.returncode != 0
    assert "home" in result.stderr.lower()


def test_env_file_fallback_resolves_unset_vars(tmp_path: Path):
    env_file = tmp_path / "robothor.env"
    env_file.write_text(f"ROBOTHOR_WORKSPACE={WS}\nROBOTHOR_SERVICE_USER={USER}\n")
    unit = tmp_path / "robothor-envfile.service"
    unit.write_text("[Service]\nUser=robothor\nWorkingDirectory=/opt/robothor\n")
    env = base_env(
        ROBOTHOR_WORKSPACE=None,
        ROBOTHOR_SERVICE_USER=None,
        ROBOTHOR_SERVICE_HOME=None,
        ROBOTHOR_ENV_FILE=str(env_file),
    )
    result = render(unit, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WorkingDirectory={WS}\n" in result.stdout
    assert f"User={USER}\n" in result.stdout


def test_unknown_robothor_var_fails_the_render(tmp_path: Path):
    """A `${ROBOTHOR_*}` spelling the renderer does not know must fail loudly —
    systemd cannot expand it, so letting it through installs a broken unit."""
    unit = tmp_path / "robothor-mystery.service"
    unit.write_text("[Service]\nUser=robothor\nExecStart=${ROBOTHOR_MYSTERY}/bin/run\n")
    result = render(unit, base_env())
    assert result.returncode != 0
    assert "ROBOTHOR_MYSTERY" in result.stderr or "placeholder" in result.stderr.lower()


# ── Installer: --root install ────────────────────────────────────────────────

EXPECTED_INSTALLED = [
    "robothor-engine.service",
    "robothor-bridge.service",
    "robothor-restart.path",
    "robothor-backup-local.timer",
    "robothor-alert@.service",
    "robothor-engine.service.d/hardening.conf",
    "robothor-engine.service.d/upgrade-rip-flags.conf",
]


def test_installs_rendered_units_into_root(tmp_path: Path):
    result = run_install(tmp_path, base_env())
    assert result.returncode == 0, result.stdout + result.stderr

    system_dir = tmp_path / "etc" / "systemd" / "system"
    for name in EXPECTED_INSTALLED:
        dest = system_dir / name
        assert dest.exists(), f"{dest} was not installed\n{result.stdout}"
        mode = dest.stat().st_mode & 0o777
        assert mode == 0o644, f"{dest} mode is {oct(mode)}, expected 0644"

    engine = directives((system_dir / "robothor-engine.service").read_text())
    assert f"ExecStart={WS}/venv/bin/python" in engine
    assert f"User={USER}" in engine
    assert "${" not in engine
    assert "%h" not in engine


def test_installer_never_installs_delphi_units(tmp_path: Path):
    result = run_install(tmp_path, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    system_dir = tmp_path / "etc" / "systemd" / "system"
    delphi = list(system_dir.rglob("delphi*"))
    assert not delphi, f"delphi units are instance-land, must not install: {delphi}"
    for installed in system_dir.rglob("*"):
        if installed.is_file():
            rel = installed.relative_to(system_dir)
            assert str(rel).startswith("robothor-"), f"unexpected install: {rel}"


def test_installer_second_run_reports_unchanged(tmp_path: Path):
    first = run_install(tmp_path, base_env())
    assert first.returncode == 0, first.stdout + first.stderr
    second = run_install(tmp_path, base_env())
    assert second.returncode == 0, second.stdout + second.stderr
    assert "unchanged" in second.stdout
    assert "updated" not in second.stdout
    assert "installed" not in second.stdout.replace("[install-units]", "")


def test_installer_restores_hand_edited_unit(tmp_path: Path):
    first = run_install(tmp_path, base_env())
    assert first.returncode == 0, first.stdout + first.stderr
    target = tmp_path / "etc" / "systemd" / "system" / "robothor-engine.service"
    pristine = target.read_text()
    target.write_text(pristine + "\n# hand edit, should be overwritten\n")

    second = run_install(tmp_path, base_env())
    assert second.returncode == 0, second.stdout + second.stderr
    assert "updated" in second.stdout
    assert target.read_text() == pristine, "a drifted installed unit must be restored"


def test_installer_fails_loudly_when_env_unresolvable(tmp_path: Path):
    result = run_install(tmp_path, base_env(ROBOTHOR_SERVICE_USER=None))
    assert result.returncode != 0
    assert "ROBOTHOR_SERVICE_USER" in result.stdout + result.stderr


def test_verify_is_skipped_under_root_test_mode(tmp_path: Path):
    """--root is test mode: systemd-analyze verify checks binaries exist on the
    target box, so it must not gate a test-root install. PATH-stub
    systemd-analyze to prove it is never invoked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "verify-was-called"
    stub = bin_dir / "systemd-analyze"
    stub.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    env = base_env()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    root = tmp_path / "root"
    result = run_install(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), "systemd-analyze verify must be skipped under --root"


# ── Repo templates ───────────────────────────────────────────────────────────


def repo_units() -> list[Path]:
    units = sorted(
        p
        for pattern in ("robothor-*.service", "robothor-*.timer", "robothor-*.path")
        for p in UNIT_DIR.glob(pattern)
    )
    units += sorted(UNIT_DIR.glob("robothor-*.service.d/*.conf"))
    assert units, "no unit templates found in infra/systemd/"
    return units


def directives(text: str) -> str:
    """Unit-file content minus comment lines — comments may legitimately
    DISCUSS a placeholder (e.g. 'never write %h here') and are passed
    through the renderer untouched."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", ";")))


@pytest.mark.parametrize("unit", repo_units(), ids=lambda p: p.name)
def test_repo_units_render_installable(unit: Path):
    """Render every repo robothor-* unit; nothing unexpanded may survive.
    This is the drift gate: a template placeholder the renderer does not
    handle means the installed unit would be broken."""
    result = render(unit, base_env())
    assert result.returncode == 0, f"{unit.name}: {result.stdout}{result.stderr}"
    rendered = directives(result.stdout)
    assert "${" not in rendered, f"{unit.name}: unexpanded ${{...}} survives rendering"
    assert "%h" not in rendered, f"{unit.name}: %h survives rendering (== /root in system units)"
    for line in rendered.splitlines():
        assert line not in ("User=robothor", "Group=robothor"), (
            f"{unit.name}: placeholder service account survives rendering"
        )
    assert "/opt/robothor" not in rendered, f"{unit.name}: workspace placeholder survives"
    assert "/home/robothor" not in rendered, f"{unit.name}: home placeholder survives"


@pytest.mark.parametrize("unit", repo_units(), ids=lambda p: p.name)
def test_repo_templates_use_canonical_spellings(unit: Path):
    """Template convention (infra/systemd/README.md): /opt/robothor is THE
    workspace placeholder, /home/robothor THE home placeholder, robothor THE
    service account. No `${ROBOTHOR_*}` (systemd cannot expand it — verify
    fails) and no `%h` (expands to /root in system units — a past incident)."""
    text = directives(unit.read_text())
    assert "${ROBOTHOR_" not in text, (
        f"{unit.name}: use /opt/robothor, not ${{ROBOTHOR_WORKSPACE}} — systemd "
        "cannot expand it and systemd-analyze verify fails on it"
    )
    assert "%h" not in text, (
        f"{unit.name}: %h is /root in system units (documented incident) — use "
        "/opt/robothor for the workspace or /home/robothor for the service home"
    )
    for line in text.splitlines():
        if line.startswith(("User=", "Group=")):
            account = line.split("=", 1)[1].strip()
            assert account in {"robothor", "postgres", "root"}, (
                f"{unit.name}: {line!r} hardcodes an instance account — use the "
                "robothor placeholder (rendered to ROBOTHOR_SERVICE_USER)"
            )
    for home in re.findall(r"/home/[A-Za-z0-9._-]+", text):
        assert home == "/home/robothor", (
            f"{unit.name}: {home} is an instance home path — use /home/robothor "
            "(the placeholder) or /opt/robothor for workspace paths"
        )
