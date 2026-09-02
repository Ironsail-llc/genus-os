"""A unit's ``EnvironmentFile=`` can hand a root script a PATH with no ``/usr/sbin``.

On 2026-09-02 the backup volume guard paged DOWN while the volume was
healable. It runs as root from ``robothor-backup-volume-guard.service``, which
loads ``EnvironmentFile=/etc/robothor/robothor.env`` — and the instance file
there sets an operator PATH::

    PATH=<user bins>:/usr/local/bin:/usr/bin:/bin

No ``/usr/sbin``, no ``/sbin``. ``dmsetup``, ``cryptsetup``, ``fsck.ext4``,
``smartctl`` and ``runuser`` all live in ``/usr/sbin``, so under systemd the
guard could not run a single one of them. ``dmsetup deps`` printed nothing
because it was never found; the guard reads that output, so "the tool is
absent" arrived as "this node is backed by nothing", it decided its own live
mapper was a stranger's, refused to heal, and paged. The same script run from
a shell healed the volume — the defect lives entirely in the environment the
unit supplies.

That environment is INSTANCE data: ``/etc/robothor/robothor.env`` is written
per box and this repo does not control it. So the invariant cannot be "the
instance file must carry a good PATH". It has to be: **every root script a
unit starts sets its own PATH before it runs anything external.**

Sets, not extends. The operator value does not merely lack ``/usr/sbin``; it
BEGINS with user-writable directories (``~/.local/bin``, ``~/.npm-global``),
and these scripts run as root. A script that appended the system directories
to what it inherited would still take its ``dmsetup`` from the first entry
that had one — so the inherited value is replaced outright.
``ROBOTHOR_EXTRA_PATH`` is a TEST-ONLY leading directory — the suites put
their stub binaries there, and it is never set in a unit or in
``/etc/robothor/robothor.env``. It is one line, identical in every root script,
so a reader who has seen it once recognises it everywhere.

This test enforces that for every script a unit with an ``EnvironmentFile=``
actually starts, so the next script added to the fleet cannot inherit the
2026-09-02 outage. ``scripts/slo_probe.sh`` and ``scripts/restore-drill.sh``
need the same prelude (``runuser`` is in ``/usr/sbin``) but are not started by
any unit in ``infra/systemd/`` — they are cron/manual entry points — so they
are outside what this test can derive, rather than allowlisted out of it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO_ROOT / "infra" / "systemd"
SCRIPTS = REPO_ROOT / "scripts"
INSTALLER = SCRIPTS / "install-host-scripts.sh"

EXEC_DIRECTIVES = ("ExecStart=", "ExecStartPre=", "ExecCondition=")

# The prelude, statement for statement. Every script carries the same block, so
# a reader who has seen it once recognises it everywhere and a drifted copy —
# one that inherits, or one that puts the opt-in directory in front outside
# the test knob — fails here rather than in production.
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PRELUDE = [
    'export PATH="${ROBOTHOR_EXTRA_PATH:+$ROBOTHOR_EXTRA_PATH:}' + SYSTEM_PATH + '"',
]


def _installed_to_repo() -> dict[str, Path]:
    """``/usr/local/bin/robothor-*.sh`` -> the repo script it is installed from.

    Read out of ``scripts/install-host-scripts.sh`` rather than hand-listed:
    the installer IS the mapping, and a hand-maintained copy of it would drift
    the way every other hand-maintained name list in this repo has.
    """
    mapping: dict[str, Path] = {}
    for src, dest in re.findall(
        r'install_one\s+"\$\{REPO_ROOT\}/scripts/([^"]+)"\s+"([^"]+)"', INSTALLER.read_text()
    ):
        mapping[f"/usr/local/bin/{dest}"] = SCRIPTS / src
    return mapping


def _script_for(token: str) -> Path | None:
    """The repo script a unit's exec token names, or None if it is not one."""
    installed = _installed_to_repo()
    if token in installed:
        return installed[token]
    if token.endswith(".sh"):
        candidate = SCRIPTS / Path(token).name
        if candidate.is_file():
            return candidate
    return None


def _exec_targets(unit: Path) -> list[Path]:
    """Repo scripts started by this unit.

    The command line may lead with an interpreter (``/usr/bin/bash``,
    ``/usr/bin/env``) or with the script itself, so every whitespace-separated
    token is offered to the resolver and only the ones that ARE repo scripts
    come back.
    """
    found: list[Path] = []
    for raw in unit.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith(EXEC_DIRECTIVES):
            continue
        for token in line.split("=", 1)[1].split():
            script = _script_for(token.strip("'\""))
            if script is not None and script not in found:
                found.append(script)
    return found


def _units_with_environment_file() -> list[Path]:
    return sorted(
        unit
        for unit in UNIT_DIR.glob("*.service")
        if any(
            line.strip().startswith("EnvironmentFile=")
            for line in unit.read_text().splitlines()
            if not line.strip().startswith("#")
        )
    )


def _statements(script: Path) -> list[str]:
    """The script's statements: no shebang, no comments, no blank lines, and
    no ``set -...`` (which touches nothing external and legitimately comes
    first). Runs of whitespace are collapsed, so the comparison below is about
    the statement and not about how it was indented or spaced."""
    out = []
    for raw in script.read_text().splitlines():
        line = re.sub(r"\s+", " ", raw.strip())
        if not line or line.startswith("#!") or line.startswith("#"):
            continue
        if re.fullmatch(r"set(\s+[-+]?[A-Za-z]+)+", line):
            continue
        out.append(line)
    return out


def missing_prelude(script: Path) -> str:
    """"" when the script sets its PATH first, else why it does not.

    A checker that can only ever return "" is the inert control this repo has
    shipped before, so it is exercised against fixtures below.
    """
    statements = _statements(script)
    head = statements[: len(PRELUDE)]
    if head == PRELUDE:
        return ""
    if PRELUDE[0] in statements:
        return (
            f"{script.name} sets its PATH, but only after "
            f"{statements[0]!r} has already run"
        )
    inheriting = [s for s in statements if s.startswith("PATH=") or s.startswith("export PATH=")]
    if inheriting:
        return (
            f"{script.name} builds its PATH some other way ({inheriting[0]!r}) — "
            "the fixed system PATH is what keeps root off a user-writable directory"
        )
    return f"{script.name} never sets a system PATH; its first statement is {statements[0]!r}"


PAIRS = sorted(
    {
        (unit.name, script)
        for unit in _units_with_environment_file()
        for script in _exec_targets(unit)
    }
)


def test_the_derivation_finds_the_scripts_it_is_supposed_to_guard():
    """A test that derives its own subjects can quietly derive none. These are
    the ones the outage was about."""
    guarded = {script.name for _, script in PAIRS}
    for expected in (
        "backup-volume-guard.sh",
        "backup-volume-check.sh",
        "liveness_probe.sh",
        "send_failure_alert.sh",
        "thermal-guard.sh",
        "thermal-shed.sh",
        "boot-guard.sh",
        "gpu-clock-cap.sh",
        "backup-ssd.sh",
        "backup-offsite.sh",
        "wal-offsite.sh",
        "pg-basebackup.sh",
    ):
        assert expected in guarded, f"{expected} is started by no EnvironmentFile= unit"


@pytest.mark.parametrize(
    ("unit", "script"), PAIRS, ids=[f"{unit}:{script.name}" for unit, script in PAIRS]
)
def test_every_unit_started_script_repairs_its_own_PATH(unit: str, script: Path):
    problem = missing_prelude(script)
    assert not problem, (
        f"{problem}\n{unit} loads an EnvironmentFile= that may carry an operator "
        "PATH with no /usr/sbin — see infra/systemd/README.md"
    )


# ── the checker itself ───────────────────────────────────────────────────────


def _fixture(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fixture.sh"
    path.write_text(body)
    return path


def test_a_script_without_the_prelude_is_reported(tmp_path: Path):
    script = _fixture(
        tmp_path,
        "#!/usr/bin/env bash\n# a comment\nset -euo pipefail\n\ndmsetup deps robothor-backup\n",
    )
    assert "never sets a system PATH" in missing_prelude(script)


def test_a_script_that_extends_the_inherited_PATH_is_reported(tmp_path: Path):
    """The tempting fix, and the one that leaves root running binaries out of
    ~/.local/bin: keep what the unit handed over and add the system
    directories to it."""
    script = _fixture(
        tmp_path,
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'export PATH="${{PATH}}:{SYSTEM_PATH}"\ndmsetup deps robothor-backup\n',
    )
    assert "builds its PATH some other way" in missing_prelude(script)


def test_a_prelude_that_comes_too_late_is_reported(tmp_path: Path):
    """First means first. A PATH repaired after the first external command has
    already run is a PATH repaired after the answer was already wrong."""
    script = _fixture(
        tmp_path,
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'DEPS="$(dmsetup deps robothor-backup)"\n' + "\n".join(PRELUDE) + "\n",
    )
    assert "only after" in missing_prelude(script)


def test_the_prelude_satisfies_the_checker(tmp_path: Path):
    script = _fixture(
        tmp_path,
        "#!/usr/bin/env bash\n# why\nset -uo pipefail\n\n" + "\n".join(PRELUDE) + "\nmain\n",
    )
    assert missing_prelude(script) == ""


# ── the one script that sources the instance file itself ─────────────────────


def test_cron_wrapper_does_not_hand_the_operator_PATH_to_the_command_it_wraps(
    tmp_path: Path,
):
    """``scripts/cron-wrapper.sh`` sources ``/etc/robothor/robothor.env`` under
    ``set -a`` — so the file that carries the operator's PATH is exported over
    the wrapper's own fixed one, and every cron job the wrapper runs (as root)
    would inherit it. Setting the PATH at the top is not enough here; it has to
    be put back after the source."""
    instance_env = tmp_path / "robothor.env"
    instance_env.write_text('PATH="/tmp/user-writable:/usr/bin:/bin"\nROBOTHOR_DB_USER=alice\n')
    secrets = tmp_path / "secrets.env"
    secrets.write_text("")  # exists, so the wrapper never runs decrypt-secrets.sh

    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "cron-wrapper.sh"),
            "bash",
            "-c",
            'printf "%s" "$PATH"',
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ["PATH"],
            "ROBOTHOR_INSTANCE_ENV": str(instance_env),
            "ROBOTHOR_SECRETS_FILE": str(secrets),
            # The wrapped command succeeds, so nothing here pages — but this
            # wrapper CAN page, and a page the suite spools is delivered for
            # real by the next liveness drain. Every sender, spool and state
            # seam is pinned under tmp_path
            # (tests/test_alert_never_pages_from_tests.py).
            "ROBOTHOR_ALERT_SUPPRESS": "1",
            "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
            "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
            "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-state"),
            "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(tmp_path / "alert-fallback"),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/tmp/user-writable" not in result.stdout, (
        "the wrapped command inherited the PATH out of the instance env file — "
        f"as root, out of a user-writable directory\nPATH was: {result.stdout}"
    )
    assert result.stdout.startswith(SYSTEM_PATH), result.stdout
