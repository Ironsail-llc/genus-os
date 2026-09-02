"""Regression test for pg-basebackup.sh's offsite group/setgid verification.

Today's incident's root cause: pg-basebackup.sh's `chgrp $GROUP $DEST` and
`chmod 2775 $DEST` can both exit 0 while the base backup directory is left
without the offsite group — because Linux silently CLEARS the setgid bit on
a chmod issued by a caller who is not a member of the target group, and
this is "not reported as an error" (man 2 chmod). A check that only looks
at chgrp/chmod's exit codes can never observe this; it must verify the
RESULT with stat.

The chmod stub below fakes exactly that kernel behavior (exit 0, setgid
silently dropped) so the test is hermetic — reproducing the real kernel
privilege check portably (across CI users/containers) is not reliable, and
is not what's under test here anyway.
"""

from __future__ import annotations

import grp
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "pg-basebackup.sh"

# The chmod stub only intercepts the exact offsite-group call
# (`chmod 2775 $DEST`); pg-basebackup.sh's later `chmod -R g+rX "$OUT"` and
# `chmod g+r ...backup_label` calls pass through untouched.
STRIPPING_CHMOD = (
    'if [[ "$1" == "2775" ]]; then\n'
    '    exec /bin/chmod 0775 "${@:2}"\n'
    "fi\n"
    'exec /bin/chmod "$@"\n'
)


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _own_group_name() -> str:
    """A real group the test process belongs to, so the (unstubbed) chgrp
    genuinely succeeds — isolating the test to chmod's behavior alone."""
    return grp.getgrgid(os.getgid()).gr_name


def _run_basebackup(
    tmp_path: Path, group: str, chmod_body: str | None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    dest = tmp_path / "mnt" / "backup" / "robothor" / "basebackup"
    dest.mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "pg_basebackup",
        'out=""\n'
        'for a in "$@"; do case "$a" in --pgdata=*) out="${a#--pgdata=}" ;; esac; done\n'
        'mkdir -p "$out"\n'
        ': > "$out/base.tar.gz"\n',
    )
    if chmod_body is not None:
        _stub(bin_dir / "chmod", chmod_body)

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROBOTHOR_BASEBACKUP_DIR": str(dest),
        "ROBOTHOR_BACKUP_GROUP": group,
        "ROBOTHOR_BASEBACKUP_KEEP": "3",
        # The `mountpoint -q` guard this script used to carry is now
        # scripts/backup-volume-check.sh, which also refuses a path on the root
        # filesystem — and a pytest tmp_path always is one. An unprivileged test
        # cannot create a real mount; tests/test_backup_volume_check.py is what
        # proves that step is armed by default.
        "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
        # Keep the last-good marker out of /var/lib/robothor.
        "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env
    )
    return result, dest


def test_warns_when_setgid_is_silently_stripped_despite_exit_zero(tmp_path: Path):
    group = _own_group_name()
    result, dest = _run_basebackup(tmp_path, group, STRIPPING_CHMOD)

    mode = dest.stat().st_mode
    assert not (mode & stat.S_ISGID), (
        "test setup broken: the chmod stub should have left setgid unset"
    )

    combined = result.stdout + result.stderr
    assert f"usermod -aG {group} postgres" in combined, (
        "chgrp/chmod both exited 0, so an exit-code-only check stays silent "
        f"even though the setgid bit never actually got set\n{combined}"
    )


def test_no_warning_when_group_and_setgid_both_actually_succeed(tmp_path: Path):
    group = _own_group_name()
    result, dest = _run_basebackup(tmp_path, group, chmod_body=None)

    mode = dest.stat().st_mode
    assert mode & stat.S_ISGID, "test setup: real chmod should have set setgid here"
    assert dest.group() == group

    combined = result.stdout + result.stderr
    assert "usermod" not in combined, combined
