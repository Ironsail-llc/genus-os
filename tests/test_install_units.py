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


def test_renders_the_database_role(tmp_path: Path):
    """`Environment=PGUSER=robothor` is the DB-account placeholder.

    systemd does NOT expand `${ROBOTHOR_DB_USER}` inside Environment= (only
    ExecStart= and friends get expansion), so a unit that needs a role has to
    carry it rendered. Without one, robothor-slo.service queried as root under
    peer auth and both DB-backed SLOs printed UNEVALUATED forever.
    """
    unit = tmp_path / "robothor-db.service"
    unit.write_text(
        "[Service]\nUser=robothor\nEnvironment=PGUSER=robothor\n"
        "Environment=NOT_A_USER=robothor\nExecStart=/opt/robothor/scripts/x.sh\n"
    )
    result = render(unit, base_env(ROBOTHOR_DB_USER="pgrole"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Environment=PGUSER=pgrole\n" in result.stdout
    assert "Environment=NOT_A_USER=robothor\n" in result.stdout, (
        "the substitution is exact-line anchored, like User=/Group="
    )


def test_database_role_defaults_to_the_service_account(tmp_path: Path):
    """One account by default — peer auth wants the OS user and the PG role to
    be the same name, which is the arrangement every other unit already has."""
    unit = tmp_path / "robothor-db.service"
    unit.write_text("[Service]\nUser=robothor\nEnvironment=PGUSER=robothor\n")
    result = render(unit, base_env(ROBOTHOR_DB_USER=None))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Environment=PGUSER={USER}\n" in result.stdout


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

#: Every mirrored drop-in directory, and the closed set of .conf files in it.
#:
#: A drop-in directory is a closed set because it is where production posture
#: lives outside the unit file. The engine's live copy had accumulated twelve
#: `.bak-*`/`.pre-*` files plus two drop-ins (onfailure, restart-forever) that
#: existed ONLY on the box — installed by hand, mirrored nowhere, therefore not
#: reproducible on a rebuild. The bridge, orchestrator and vision directories
#: were the same story one unit over: seven hand-written .conf files, two of
#: which (zz-rls.conf) carry the RLS posture and one (zz-bind-loopback.conf)
#: the fix for an unauthenticated 0.0.0.0 bind. Pinning the sets here means a
#: new drop-in has to be added deliberately, in a reviewed diff.
EXPECTED_DROPINS: dict[str, set[str]] = {
    "robothor-engine.service.d": {
        "boot-guard.conf",
        "hardening.conf",
        "onfailure.conf",
        "restart-forever.conf",
        "upgrade-rip-flags.conf",
        "zz-sandbox.conf",
    },
    "robothor-bridge.service.d": {
        "onfailure.conf",
        "restart-forever.conf",
    },
    "robothor-orchestrator.service.d": {
        "instance-env.conf",
        "onfailure.conf",
        "restart-forever.conf",
        "zz-bind-loopback.conf",
        "zz-rls.conf",
    },
    "robothor-vision.service.d": {
        "onfailure.conf",
        "restart-forever.conf",
        "zz-rls.conf",
    },
}

EXPECTED_INSTALLED = [
    "robothor-engine.service",
    "robothor-bridge.service",
    "robothor-restart.path",
    "robothor-backup-local.timer",
    "robothor-alert@.service",
    "robothor-liveness.service",
    "robothor-liveness.timer",
    "robothor-bench-rotation.service",
    "robothor-bench-rotation.timer",
    "robothor-backup-volume-guard.service",
    "robothor-backup-volume-guard.timer",
] + [f"{d}/{conf}" for d, confs in EXPECTED_DROPINS.items() for conf in sorted(confs)]


def test_the_set_of_mirrored_dropin_dirs_is_closed():
    """A drop-in directory that appears in infra/systemd/ without a line in
    EXPECTED_DROPINS is a directory nothing pins — which is how the engine's
    grew two untracked files."""
    present = {p.name for p in UNIT_DIR.glob("robothor-*.service.d") if p.is_dir()}
    assert present == set(EXPECTED_DROPINS)


@pytest.mark.parametrize("dirname", sorted(EXPECTED_DROPINS), ids=lambda d: d)
def test_dropin_dir_is_a_closed_set(dirname: str):
    """Exactly the pinned .conf mirrors, no more and no fewer."""
    present = {p.name for p in (UNIT_DIR / dirname).glob("*.conf")}
    assert present == EXPECTED_DROPINS[dirname]


@pytest.mark.parametrize("dirname", sorted(EXPECTED_DROPINS), ids=lambda d: d)
def test_dropin_dir_carries_no_backup_files(dirname: str):
    """`.bak-*`/`.pre-*` copies are how the live directory became unreadable;
    they must never be mirrored into the repo."""
    strays = [p.name for p in (UNIT_DIR / dirname).iterdir() if p.is_file() and p.suffix != ".conf"]
    assert not strays, f"backup/scratch files in the drop-in mirror: {strays}"


ONFAILURE_MIRRORS = sorted(d for d, confs in EXPECTED_DROPINS.items() if "onfailure.conf" in confs)


@pytest.mark.parametrize("dirname", ONFAILURE_MIRRORS, ids=lambda d: d)
def test_onfailure_dropin_matches_its_other_installer_byte_for_byte(dirname: str):
    """Two installers write this file — scripts/install_onfailure_alerts.sh
    (for every paged unit) and scripts/install-units.sh (from these mirrors).
    If they disagree by one byte they overwrite each other in turn and the
    drift check flaps forever, so every mirror is pinned to the generator's
    heredoc."""
    generator = (REPO_ROOT / "scripts" / "install_onfailure_alerts.sh").read_text()
    body = generator.split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0] + "\n"
    mirror = (UNIT_DIR / dirname / "onfailure.conf").read_text()
    assert mirror == body


def test_restart_forever_dropins_agree_on_their_directives():
    """The comment header explains why THIS unit restarts forever and so
    differs per unit; the directives must not. A drop-in that sets a different
    key is a drop-in that does something else and should not share the name."""
    bodies = {
        dirname: directives((UNIT_DIR / dirname / "restart-forever.conf").read_text()).strip()
        for dirname, confs in EXPECTED_DROPINS.items()
        if "restart-forever.conf" in confs
    }
    assert len(bodies) == 4, bodies
    assert len(set(bodies.values())) == 1, bodies
    assert set(bodies.values()) == {"[Unit]\nStartLimitIntervalSec=0"}, bodies


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
    """Git-TRACKED unit templates only. Gitignored instance units (e.g. a
    local delphi service with a real User=) legitimately sit in infra/systemd/
    on a dev box; they are not platform templates and must not fail the
    template gates."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "infra/systemd"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        cwd=REPO_ROOT,
    ).stdout.splitlines()
    tracked_paths = {REPO_ROOT / line for line in tracked if line}
    units = sorted(
        p
        for pattern in ("robothor-*.service", "robothor-*.timer", "robothor-*.path")
        for p in UNIT_DIR.glob(pattern)
        if p in tracked_paths
    )
    units += sorted(p for p in UNIT_DIR.glob("robothor-*.service.d/*.conf") if p in tracked_paths)
    assert units, "no tracked unit templates found in infra/systemd/"
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


def test_execstart_never_relies_on_bare_env_lookup():
    """`/usr/bin/env python` (or any bare command) is unrunnable under
    systemd: units get PATH=/usr/bin:/bin, which has no `python` on Ubuntu
    and no venv entry points. Every ExecStart must use an absolute
    interpreter/binary path (env is fine when it only sets VAR=val before an
    absolute path). Live incident 2026-08-20: installing the templates took
    down the bridge and orchestrator with status=127."""
    for unit in repo_units():
        # join systemd line continuations so multiline ExecStarts parse whole
        text = unit.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            if not line.startswith(("ExecStart=", "ExecStartPre=")):
                continue
            cmd = line.split("=", 1)[1].lstrip("-@:+!").strip()
            parts = cmd.split()
            if not parts:
                continue
            if parts[0].endswith("/env"):
                # env is fine only for VAR=val assignments before an absolute path
                real = next((p for p in parts[1:] if "=" not in p or p.startswith("/")), "")
                assert real.startswith("/"), (
                    f"{unit.name}: {line!r} relies on PATH lookup of {real!r} — "
                    "systemd units must use absolute paths"
                )
            else:
                assert parts[0].startswith("/"), (
                    f"{unit.name}: {line!r} does not use an absolute path"
                )


# ── Repo tmpfiles templates ──────────────────────────────────────────────────
# infra/systemd/ has been gated since the renderer landed; infra/tmpfiles/ never
# was. So infra/tmpfiles/robothor-restart.conf shipped with a real operator
# username in its positional user/group columns, and nothing caught it:
#
#   * render-unit.sh's account rules are anchored to exact `User=`/`Group=`
#     lines, and a tmpfiles row's account columns are POSITIONAL.
#   * check_instance_leak.py's /home/<user>/ pattern does not match a bare name.
#   * install-units.sh copied the file RAW, deliberately bypassing the renderer,
#     so on any other instance the request directory would be chowned to an
#     account that does not exist and the restart broker would silently break.

TMPFILES_DIR = REPO_ROOT / "infra" / "tmpfiles"

# Mirrors the set in test_repo_templates_use_canonical_spellings. `robothor` is
# the PLACEHOLDER (rendered to $ROBOTHOR_SERVICE_USER); postgres and root exist
# on every box; `-` means "leave it alone".
CANONICAL_ACCOUNTS = {"robothor", "postgres", "root", "-"}


def repo_tmpfiles() -> list[Path]:
    """Git-TRACKED tmpfiles.d templates."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "infra/tmpfiles"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        cwd=REPO_ROOT,
    ).stdout.splitlines()
    confs = sorted(REPO_ROOT / line for line in tracked if line.endswith(".conf"))
    assert confs, "no tracked tmpfiles templates found in infra/tmpfiles/"
    return confs


def tmpfiles_rows(text: str) -> list[list[str]]:
    """Field-split non-comment rows: TYPE PATH MODE USER GROUP AGE ARGUMENT."""
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 5 and fields[1].startswith("/"):
            rows.append(fields)
    return rows


def render_tmpfiles(src: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RENDER), "--tmpfiles", str(src)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.parametrize("conf", repo_tmpfiles(), ids=lambda p: p.name)
def test_repo_tmpfiles_use_canonical_account(conf: Path):
    """An instance username in a positional account column is invisible to
    every other gate in this file."""
    rows = tmpfiles_rows(conf.read_text())
    assert rows, f"{conf.name}: no tmpfiles rows parsed — check the format"
    for fields in rows:
        for column, value in (("user", fields[3]), ("group", fields[4])):
            assert value in CANONICAL_ACCOUNTS, (
                f"{conf.name}: {column} column names {value!r}, an instance "
                "account — use the `robothor` placeholder (rendered to "
                "ROBOTHOR_SERVICE_USER by render-unit.sh --tmpfiles)"
            )


@pytest.mark.parametrize("conf", repo_tmpfiles(), ids=lambda p: p.name)
def test_repo_tmpfiles_render_installable(conf: Path):
    """The drift gate: the placeholder must actually be substituted, or the
    runtime directory is chowned to an account that may not exist."""
    result = render_tmpfiles(conf, base_env())
    assert result.returncode == 0, f"{conf.name}: {result.stdout}{result.stderr}"
    for fields in tmpfiles_rows(result.stdout):
        assert fields[3] in (USER, "-"), f"{conf.name}: user column {fields[3]!r}"
        assert fields[4] in (USER, "-"), f"{conf.name}: group column {fields[4]!r}"
        assert "robothor" not in (fields[3], fields[4]), (
            f"{conf.name}: placeholder account survives rendering"
        )


@pytest.mark.parametrize("conf", repo_tmpfiles(), ids=lambda p: p.name)
def test_repo_tmpfiles_have_no_instance_home_paths(conf: Path):
    text = "\n".join(
        line for line in conf.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    for home in re.findall(r"/home/[A-Za-z0-9._-]+", text):
        assert home == "/home/robothor", f"{conf.name}: {home} is an instance home path"


def test_tmpfiles_runtime_paths_are_not_mangled(tmp_path: Path):
    """/run/robothor is a real runtime path, not a placeholder. Guards against
    'fixing' the positional-account problem with a blanket word swap."""
    conf = tmp_path / "robothor-x.conf"
    conf.write_text("d /run/robothor/requests 0700 robothor robothor -\n")
    result = render_tmpfiles(conf, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/run/robothor/requests" in result.stdout


def test_tmpfiles_other_accounts_are_untouched(tmp_path: Path):
    conf = tmp_path / "robothor-pg.conf"
    conf.write_text("d /run/pg 0700 postgres postgres -\n")
    result = render_tmpfiles(conf, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "postgres postgres" in result.stdout


def test_tmpfiles_flag_does_not_change_unit_rendering(sample_unit: Path):
    """--tmpfiles must not alter unit-file behaviour; the rules are disjoint."""
    plain = render(sample_unit, base_env())
    flagged = render_tmpfiles(sample_unit, base_env())
    assert plain.stdout == flagged.stdout


def test_positional_account_is_invisible_without_the_flag(tmp_path: Path):
    """Documents the bug the flag exists for: render-unit.sh's User=/Group=
    rules are exact-line anchored, so a tmpfiles conf through the PLAIN
    renderer emits the placeholder verbatim — which is why piping it through
    unchanged would have looked fixed while chowning to a bogus account."""
    conf = tmp_path / "robothor-x.conf"
    conf.write_text("d /run/x 0700 robothor robothor -\n")
    result = render(conf, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "robothor robothor" in result.stdout, (
        "if this now renders, the flag is redundant — delete it and this test"
    )


def test_installs_every_tracked_tmpfiles_conf(tmp_path: Path):
    """The installer used to name robothor-restart.conf literally, so the
    SECOND tmpfiles template (robothor-backup-state.conf, which creates the
    last-good marker directory the backup guard reads) would have been added to
    the repo, gated by every test above, and never installed on any box — an
    inert control with a full set of passing tests.
    """
    result = run_install(tmp_path, base_env())
    assert result.returncode == 0, result.stdout + result.stderr
    installed = tmp_path / "etc/tmpfiles.d"
    for conf in repo_tmpfiles():
        dest = installed / conf.name
        assert dest.exists(), f"{conf.name} was never installed\n{result.stdout}"
        assert dest.stat().st_mode & 0o777 == 0o644
        rows = tmpfiles_rows(dest.read_text())
        assert rows, f"{conf.name}: no tmpfiles row installed"
        for fields in rows:
            assert fields[3] in (USER, "-") and fields[4] in (USER, "-"), (
                f"{conf.name}: unrendered account {fields}"
            )


def test_installs_the_privileged_helper_and_tmpfiles_conf(tmp_path: Path):
    """The broker needs both, and neither is a systemd unit — so nothing else
    in this file covers them. Delete both installer blocks and every other test
    here still passes."""
    result = run_install(tmp_path, base_env())
    assert result.returncode == 0, result.stdout + result.stderr

    helper = tmp_path / "usr/local/lib/robothor/robothor-restart-handler.sh"
    assert helper.exists(), result.stdout
    assert helper.stat().st_mode & 0o777 == 0o755

    conf = tmp_path / "etc/tmpfiles.d/robothor-restart.conf"
    assert conf.exists(), result.stdout
    assert conf.stat().st_mode & 0o777 == 0o644
    rows = tmpfiles_rows(conf.read_text())
    assert rows, f"no tmpfiles row installed:\n{conf.read_text()}"
    fields = rows[0]
    assert fields[3] == USER and fields[4] == USER, f"unrendered account: {fields}"
