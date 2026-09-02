"""The backup volume guard: detect, self-heal, and page ONCE, truthfully.

The encrypted USB backup SSD drops off the bus. It has done it three times in
nine days (2026-07-14, 2026-08-24, 2026-08-27). What that leaves behind is not
a clean absence: the mount stays a mount, ``df`` still reports the cached
capacity, and the device-mapper node keeps a kernel reference so it cannot even
be closed. ext4 flips to ``emergency_ro`` and every write goes nowhere.

Before ``scripts/backup-volume-check.sh`` the four backup units ran anyway and
failed — ``robothor-wal-offsite`` every 15 minutes, 96 failures a day, ~22
Telegram pages whose entire content was a unit name. The ExecCondition= probe
turned that storm into SILENCE: the units now skip. Silence is the correct
behaviour for a backup unit and the WRONG behaviour for the fleet, because
nothing at all then says the backups have stopped.

``scripts/backup-volume-guard.sh`` is the thing that says so. Every 10 minutes
it asks the probe whether the volume is usable and, when it is not:

  * performs the recovery that worked by hand twice on this box — lazy unmount,
    reopen the LUKS container under a NEW mapper name (the stale one cannot be
    closed), ``fsck.ext4 -p``, remount at the same path — and
  * pages the operator ONCE with what has actually stopped and when each tier
    of backup last worked, then stays quiet for a day.

The tests below are about the two ways this class of control fails:

1. IT DOES NOTHING (the inert control). Every heal step is asserted on the
   real argv of a fake ``cryptsetup``/``fsck.ext4``/``mount`` on PATH, so a
   guard that logs "recovered" without touching the device fails here.
2. IT PAGES FOREVER (the muted pager). Down twice is one page, not two; a
   heal is always a page, because a bridge that flaps must never be masked.

Nothing here touches the real volume: every device, mapper, crypttab and
mountpoint is a path under tmp_path, and every external binary is a stub on
PATH that records its argv. ``ROBOTHOR_ALERT_SUPPRESS`` and a dead
``ROBOTHOR_TELEGRAM_API_BASE`` are set even though the pager is stubbed, so
that a future refactor which reaches the real ``send_failure_alert.sh`` still
cannot deliver a page (tests/test_alert_never_pages_from_tests.py).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "backup-volume-guard.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"
SERVICE = UNIT_DIR / "robothor-backup-volume-guard.service"
TIMER = UNIT_DIR / "robothor-backup-volume-guard.timer"
TMPFILES = REPO_ROOT / "infra" / "tmpfiles" / "robothor-backup-state.conf"

MAPPER = "robothor-backup"
UUID = "1a2b3c4d-0000-4000-8000-abcdefabcdef"


# ── the fake box ─────────────────────────────────────────────────────────────


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Box:
    """A whole fake backup volume: devices, mapper nodes, crypttab, markers,
    and a stub for every external binary the guard shells out to.

    One log file records every stub invocation as ``<name> <argv...>``, so a
    test can assert the guard performed the recovery rather than only claiming
    to have performed it.
    """

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "argv.log"
        self.dev_dir = tmp_path / "dev-by-uuid"
        self.mapper_dir = tmp_path / "dev-mapper"
        self.dev_dir.mkdir()
        self.mapper_dir.mkdir()
        self.mount = tmp_path / "mnt"
        self.mount.mkdir()
        self.state_dir = tmp_path / "volume-guard"
        self.backup_state_dir = tmp_path / "backup-state"
        self.backup_state_dir.mkdir()

        # The real crypttab names a keyfile: a timer has no console, so column
        # 3 is the only way the container can ever be reopened. `none` there is
        # a real configuration, and a materially different one — see
        # ``no_keyfile`` and the tests that use it.
        self.keyfile = tmp_path / "backup.key"
        self.keyfile.write_text("not-a-real-key\n")
        self.crypttab = tmp_path / "crypttab"
        self._write_crypttab(str(self.keyfile))

        self.device = self.dev_dir / UUID
        self.check_log = tmp_path / "check.log"
        self.check_count = tmp_path / "check.count"
        self.mount_count = tmp_path / "mount.count"
        self.dm_count = tmp_path / "dmsetup.count"
        self.alert_log = tmp_path / "alert.log"

        self._install_stubs()

    # -- stubs -----------------------------------------------------------
    def _stub(self, name: str, body: str) -> None:
        _script(
            self.bin / name,
            f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "{self.log}"\n{body}\n',
        )

    def _install_stubs(self) -> None:
        # findmnt is asked ONE question: what is mounted at the path.
        # FAKE_MOUNT_SOURCE defaults to the guard's OWN mapper, so every other
        # test drives the normal path; it may hold several newline-separated
        # rows, which is what stacked mounts look like.
        #
        # FAKE_MOUNTED_RC is findmnt's exit status, and the stub prints nothing
        # unless it is 0 — as the real one does. That matters, because findmnt
        # exits 1 both for "nothing is mounted there" and for a genuine error
        # (findmnt(8) EXIT STATUS), so only the OUTPUT separates the two and a
        # fake that printed a row while exiting non-zero would let the guard
        # pass a test the real tool cannot.
        self._stub(
            "findmnt",
            'if [ "${FAKE_MOUNTED_RC:-0}" = 0 ]; then\n'
            '  case " $* " in\n'
            '    *" SOURCE "*) printf "%s\\n" "${FAKE_MOUNT_SOURCE:-'
            f"$FAKE_MAPPER_DIR/{MAPPER}"
            '}" ;;\n'
            "  esac\n"
            "fi\n"
            'exit "${FAKE_MOUNTED_RC:-0}"',
        )
        self._stub("umount", 'exit "${FAKE_UMOUNT_RC:-0}"')
        # FAKE_MOUNT_RC is a LIST, one rc per invocation (last value repeats),
        # because one heal can legitimately mount twice: the cheap remount of
        # the mapping already there, then again after a repair.
        self._stub(
            "mount",
            f'n=$(cat "{self.mount_count}" 2>/dev/null || echo 0); n=$((n + 1))\n'
            f'printf "%s" "$n" > "{self.mount_count}"\n'
            'read -r -a rcs <<<"${FAKE_MOUNT_RC:-0}"\n'
            "i=$((n - 1)); (( i >= ${#rcs[@]} )) && i=$(( ${#rcs[@]} - 1 ))\n"
            'exit "${rcs[$i]}"',
        )
        self._stub("fsck.ext4", 'exit "${FAKE_FSCK_RC:-0}"')
        self._stub("smartctl", 'printf "%s\\n" "${FAKE_SMART_OUT:-SMART Health Status: OK}"')
        # FAKE_UNIT_STATE answers for every unit; FAKE_ACTIVATING names ONE
        # unit that is mid-run, so a test can pin the deferral list member by
        # member instead of putting the whole fleet in the same state.
        self._stub(
            "systemctl",
            'if [ -n "${FAKE_ACTIVATING:-}" ] && [ "$2" = "$FAKE_ACTIVATING" ]; then\n'
            '  printf "activating\\n"\n'
            "else\n"
            '  printf "%s\\n" "${FAKE_UNIT_STATE:-inactive}"\n'
            "fi",
        )
        # FAKE_DM_OPEN is a LIST, one open count per `dmsetup info` (last value
        # repeats), because a single heal asks the kernel more than once and the
        # answer is allowed to differ between the questions — a count that can
        # change is the entire reason it is re-read rather than remembered.
        # `dmsetup deps` answers PER NODE: FAKE_DM_DEPS is the default and
        # FAKE_DM_DEPS_MAP ("<name>=<majmin> <name>=<majmin>") overrides it for
        # individual nodes, because the interesting states have more than one
        # mapper node and they are backed by different things — a stale corpse
        # and the device's own live mapping side by side.
        #
        # `dmsetup info -o uuid` answers with the dm-crypt UUID, which for a
        # LUKS mapping is CRYPT-LUKS2-<container uuid, dashes stripped>-<name>.
        # That is the container's own identity and it survives the device
        # dropping off the bus, so it is how the guard tells its OWN corpse
        # from a stranger wearing its name. It deliberately does NOT bump the
        # open-count sequence: it is a different question.
        self._stub(
            "dmsetup",
            'for dm_name in "$@"; do :; done\n'
            'dm_deps="${FAKE_DM_DEPS:-8:16}"\n'
            'for kv in ${FAKE_DM_DEPS_MAP:-}; do\n'
            '  [ "${kv%%=*}" = "$dm_name" ] && dm_deps="${kv#*=}"\n'
            "done\n"
            'case "$1" in\n'
            '  info) case " $* " in\n'
            '          *" uuid "*)\n'
            '            printf "%s\\n" "${FAKE_DM_UUID:-CRYPT-LUKS2-'
            + UUID.replace("-", "")
            + '-$dm_name}" ;;\n'
            f'          *) n=$(cat "{self.dm_count}" 2>/dev/null || echo 0); n=$((n + 1))\n'
            f'             printf "%s" "$n" > "{self.dm_count}"\n'
            '             read -r -a counts <<<"${FAKE_DM_OPEN:-0}"\n'
            "             i=$((n - 1)); (( i >= ${#counts[@]} )) && i=$(( ${#counts[@]} - 1 ))\n"
            '             printf "%s\\n" "${counts[$i]}" ;;\n'
            "        esac ;;\n"
            '  deps) printf "1 dependencies : (%s)\\n" "$dm_deps" ;;\n'
            "esac",
        )
        self._stub(
            "lsblk",
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    MAJ:MIN) printf "%s\\n" "${FAKE_MAJMIN:-8:17}"; exit 0 ;;\n'
            '    PKNAME)  printf "%s\\n" "${FAKE_PKNAME:-sdb}"; exit 0 ;;\n'
            "  esac\n"
            "done",
        )
        # `cryptsetup luksUUID` reads the container's UUID out of the header.
        # It is read-only, and it is the only place that UUID exists when
        # crypttab names a device PATH instead of UUID=<…>.
        self._stub(
            "cryptsetup",
            'case "$1" in\n'
            '  isLuks) exit "${FAKE_ISLUKS_RC:-0}" ;;\n'
            '  luksUUID) rc="${FAKE_LUKSUUID_RC:-0}"\n'
            '          [ "$rc" = 0 ] && printf "%s\\n" "${FAKE_LUKS_UUID:-'
            + UUID
            + '}"\n'
            '          exit "$rc" ;;\n'
            '  open)   rc="${FAKE_OPEN_RC:-0}"\n'
            '          [ "$rc" = 0 ] && : > "$FAKE_MAPPER_DIR/$3"\n'
            '          exit "$rc" ;;\n'
            # A close that FAILS leaves the node exactly where it was — that
            # kernel reference is the whole reason a new name is needed.
            '  close)  rc="${FAKE_CLOSE_RC:-0}"\n'
            '          [ "$rc" = 0 ] && rm -f "$FAKE_MAPPER_DIR/$2"\n'
            '          exit "$rc" ;;\n'
            "esac\n"
            "exit 0",
        )
        # The volume probe. Its exit codes come from FAKE_CHECK_RCS, one per
        # invocation (last value repeats), so one run can be unhealthy on the
        # first probe and healthy on the post-heal re-probe.
        _script(
            self.root / "fake-check.sh",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "check $*" >> "{self.check_log}"\n'
            f'n=$(cat "{self.check_count}" 2>/dev/null || echo 0); n=$((n + 1))\n'
            f'printf "%s" "$n" > "{self.check_count}"\n'
            'read -r -a rcs <<<"${FAKE_CHECK_RCS:-0}"\n'
            "i=$((n - 1)); (( i >= ${#rcs[@]} )) && i=$(( ${#rcs[@]} - 1 ))\n"
            'exit "${rcs[$i]}"\n',
        )
        # The pager. Records key and body verbatim; exit code from FAKE_ALERT_RC.
        _script(
            self.root / "fake-alert.sh",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "---PAGE---" >> "{self.alert_log}"\n'
            f'printf "%s\\n" "$@" >> "{self.alert_log}"\n'
            'exit "${FAKE_ALERT_RC:-0}"\n',
        )

    # -- fixtures --------------------------------------------------------
    def _write_crypttab(self, keyfile: str) -> None:
        self.crypttab.write_text(
            f"# <name>  <device>  <keyfile>  <options>\n"
            f"{MAPPER}  UUID={UUID}  {keyfile}  luks,noauto\n"
        )

    def crypttab_names_a_path(self) -> None:
        """crypttab column 2 is a device PATH, not ``UUID=<…>``.

        A perfectly ordinary configuration, and the file then carries no
        container UUID at all — so the identity check that recognises this
        guard's own corpse has nothing to compare against unless the header is
        asked directly."""
        self.crypttab.write_text(
            "# <name>  <device>  <keyfile>  <options>\n"
            f"{MAPPER}  {self.device}  {self.keyfile}  luks,noauto\n"
        )

    def no_keyfile(self, column3: str = "none") -> None:
        """crypttab column 3 says the container is unlocked interactively —
        which a systemd timer cannot do."""
        self._write_crypttab(column3)

    def plug_in(self) -> None:
        """The USB device is present on the bus."""
        self.device.write_text("")

    def stale_mapper(self) -> None:
        """A mapper node left behind by a drop: it exists, and its backing
        major:minor no longer matches the device that just came back."""
        (self.mapper_dir / MAPPER).write_text("")

    def mapper_node(self, name: str) -> None:
        """Any other device-mapper node — a previous heal's ``<name>-1``, or
        the ``<name>-b`` a human recovered under at 3am."""
        (self.mapper_dir / name).write_text("")

    def markers(self) -> None:
        for name, value in (
            ("last-local-dump", "2026-09-01T04:30:11+02:00 robothor_memory-20260901.sql.gz"),
            ("last-offsite-ok", "2026-09-01T05:30:02+02:00 robothor_memory-20260901.sql.gz"),
            ("last-wal-offsite-ok", "2026-09-02T15:15:07+02:00 00000001000000A2000000F3"),
            ("last-basebackup", "2026-08-31T02:00:44+02:00 base-20260831"),
        ):
            (self.backup_state_dir / name).write_text(value + "\n")

    # -- running ---------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ROBOTHOR_", "FAKE_"))}
        env.update(
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "HOME": str(self.root),
                "ROBOTHOR_BACKUP_MOUNT": str(self.mount),
                "ROBOTHOR_VOLUME_GUARD_STATE_DIR": str(self.state_dir),
                "ROBOTHOR_BACKUP_STATE_DIR": str(self.backup_state_dir),
                "ROBOTHOR_CRYPTTAB": str(self.crypttab),
                "ROBOTHOR_VOLUME_GUARD_MAPPER": MAPPER,
                "ROBOTHOR_VOLUME_GUARD_DEV_DIR": str(self.dev_dir),
                "ROBOTHOR_VOLUME_GUARD_MAPPER_DIR": str(self.mapper_dir),
                "ROBOTHOR_VOLUME_GUARD_CHECK_CMD": f"bash {self.root / 'fake-check.sh'}",
                "ROBOTHOR_VOLUME_GUARD_ALERT_CMD": f"bash {self.root / 'fake-alert.sh'}",
                "FAKE_MAPPER_DIR": str(self.mapper_dir),
                # Belt and braces: neither can be reached with the pager
                # stubbed, but a refactor that reaches the real sender must
                # still be unable to deliver anything — including the durable
                # state it writes before it ever gets to the network (see
                # tests/test_alert_never_pages_from_tests.py).
                "ROBOTHOR_ALERT_SUPPRESS": "1",
                "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
                "ROBOTHOR_ALERT_SPOOL_DIR": str(self.root / "alert-spool"),
                "ROBOTHOR_ALERT_STATE_DIR": str(self.root / "alert-state"),
                "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(self.root / "alert-fallback"),
                "ROBOTHOR_SECRETS_FILE": str(self.root / "no-such-secrets.env"),
            }
        )
        env.update(extra)
        return env

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        # FAKE_CHECK_RCS describes ONE guard run (probe, then post-heal
        # re-probe), so the invocation counter resets per run. So do the mount
        # and dmsetup counters, for the same reason.
        self.check_count.unlink(missing_ok=True)
        self.mount_count.unlink(missing_ok=True)
        self.dm_count.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(GUARD)],
            capture_output=True,
            text=True,
            timeout=120,
            env=self.env(**extra),
        )

    # -- assertions ------------------------------------------------------
    @property
    def argv(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def ran(self, prefix: str) -> list[str]:
        return [line for line in self.argv if line.startswith(prefix)]

    @property
    def pages(self) -> list[str]:
        """Each page as one string: the dedup key, then the body."""
        if not self.alert_log.exists():
            return []
        chunks = self.alert_log.read_text().split("---PAGE---\n")
        return [c for c in (chunk.strip() for chunk in chunks) if c]

    def state(self, name: str) -> str | None:
        path = self.state_dir / name
        return path.read_text().strip() if path.exists() else None


@pytest.fixture
def box(tmp_path: Path) -> Box:
    b = Box(tmp_path)
    b.markers()
    return b


# ── healthy: the guard must be invisible ─────────────────────────────────────


def test_healthy_volume_pages_nothing_and_touches_nothing(box: Box):
    """The 10-minute steady state. A guard that runs 144 times a day must cost
    nothing and say nothing when the disk is fine — and above all must not
    unmount a working volume."""
    box.plug_in()
    result = box.run(FAKE_CHECK_RCS="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.pages == [], f"paged on a healthy volume: {box.pages}"
    assert box.ran("umount") == [], "unmounted a HEALTHY backup volume"
    assert box.ran("cryptsetup") == []
    assert box.ran("mount") == []
    assert box.state("down_since") is None


# ── down, device gone: one page, then quiet ──────────────────────────────────


def test_device_absent_pages_once_and_takes_no_action(box: Box):
    """The drive is off the bus. There is nothing to heal — say so, once, and
    do not touch the mapper or the mount."""
    result = box.run(FAKE_CHECK_RCS="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    page = box.pages[0]
    assert "BACKUP VOLUME DOWN" in page
    assert "device absent from USB" in page
    assert box.ran("cryptsetup") == [], "touched the mapper with no device present"
    assert box.ran("mount") == []
    assert box.state("down_since"), "the guard did not record when the volume went down"


def test_second_run_while_still_down_does_not_page_again(box: Box):
    """96 pages a day for one unfixed condition is a muted pager. The repage
    interval is a day by default."""
    first = box.run(FAKE_CHECK_RCS="1")
    assert first.returncode == 0, first.stdout + first.stderr
    second = box.run(FAKE_CHECK_RCS="1")
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(box.pages) == 1, f"repaged inside the repage window: {box.pages}"


def test_repage_interval_elapsed_pages_again(box: Box):
    """Still down a day later is news again — the outage has not been fixed."""
    box.run(FAKE_CHECK_RCS="1")
    box.run(FAKE_CHECK_RCS="1", ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS="0")
    assert len(box.pages) == 2, f"expected a repage after the interval: {box.pages}"


def test_the_down_page_names_what_stopped_and_what_did_not(box: Box):
    """~50 pages reading "🔴 <unit> FAILED" were scrolled past during an outage
    in which every backup path was down. The page has to carry the consequence:
    which tier stopped, when each last worked, and what is still running."""
    box.run(FAKE_CHECK_RCS="1")
    page = box.pages[0]
    assert "Paused: nightly dump" in page
    assert "offsite refresh" in page
    assert "base backup + WAL prune" in page
    assert "PITR RPO intact, dump-tier RPO growing" in page
    assert "Runbook: BACKUP_VOLUME_GUARD.md" in page
    # The last-good facts come from the NVMe markers, never from the volume
    # that just failed.
    assert "robothor_memory-20260901.sql.gz" in page
    assert "00000001000000A2000000F3" in page


def test_a_missing_backup_state_library_fails_the_run_instead_of_paging_blanks(
    box: Box, tmp_path: Path
):
    """``source backup-state.sh`` was unguarded and the script has no ``set
    -e``: a missing or unreadable library left every ``LAST_*`` an empty
    string, and the page then read

        Local dump last good:
        Offsite last OK:

    which an operator scans as "blank, so nothing to worry about" — the
    opposite of the truth. The library IS the guard's only source of last-good
    facts; without it there is no honest page to send, so the run fails and
    the unit's own OnFailure= pages instead.
    """
    lonely = tmp_path / "no-library"
    lonely.mkdir()
    copy = lonely / GUARD.name
    copy.write_bytes(GUARD.read_bytes())

    result = subprocess.run(
        ["bash", str(copy)],
        capture_output=True,
        text=True,
        timeout=60,
        env=box.env(FAKE_CHECK_RCS="1"),
    )
    assert result.returncode == 1, (
        f"a guard with no last-good facts exited {result.returncode} — it would "
        f"page timestamps it does not have\n{result.stdout}{result.stderr}"
    )
    assert "backup-state.sh" in result.stderr
    assert "cannot report last-good facts" in result.stderr
    assert box.pages == [], f"paged with no facts to page: {box.pages}"


def test_the_page_says_which_HALF_of_wal_offsite_is_still_running(box: Box):
    """"Still running: WAL offsite" was not true, and the guard's own code says
    so: robothor-wal-offsite.service is in BACKUP_UNITS precisely because it
    writes to the volume. What survives a wedged volume is the archiving of NEW
    WAL segments to the remote; what stops with everything else is the
    base-backup copy and the WAL prune (wal-offsite.sh reads the prune horizon
    off the volume and refuses to guess it).

    An operator who reads "WAL offsite: still running" concludes PITR is whole.
    It is — for now — and only because the segments are still going out; the
    base backup they replay onto is not being copied.
    """
    box.run(FAKE_CHECK_RCS="1")
    page = box.pages[0]
    assert "Still running: WAL offsite replication of NEW segments" in page, page
    assert "base-backup copy and WAL prune paused" in page, page
    assert "PITR RPO intact, dump-tier RPO growing" in page


def test_missing_markers_say_so_rather_than_reading_as_recent(box: Box):
    """An empty value where a timestamp belongs reads as "just now"."""
    for marker in box.backup_state_dir.iterdir():
        marker.unlink()
    box.run(FAKE_CHECK_RCS="1")
    assert "unknown (no successful run recorded)" in box.pages[0]


# ── down, device back: heal ──────────────────────────────────────────────────


def test_stale_mapping_is_reopened_under_a_new_name_and_remounted(box: Box):
    """The recovery that worked by hand, twice.

    The device came back but the old mapper node still points at the
    major:minor it had before the drop and cannot be closed (kernel
    reference), so the container is opened under ``<name>-1`` and mounted at
    the SAME path — the units bind to the path, not the mapper.
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS="8:16",  # what the mapper is backed by now
        FAKE_MAJMIN="8:17",  # what the device actually is: stale
        FAKE_DM_OPEN="1",  # held by the kernel; cannot be closed
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran(f"umount -l {box.mount}"), f"no lazy unmount:\n{box.argv}"
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), (
        f"the container was not reopened under a fresh mapper name:\n{box.argv}"
    )
    fsck = box.ran(f"fsck.ext4 -p {box.mapper_dir / (MAPPER + '-1')}")
    assert fsck, f"no preen fsck before mounting:\n{box.argv}"
    assert box.ran(f"mount {box.mapper_dir / (MAPPER + '-1')} {box.mount}"), (
        f"never remounted at the original path:\n{box.argv}"
    )

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    page = box.pages[0]
    assert "auto-recovered" in page
    assert "USB drop #1 since boot" in page
    assert f"remapped as {MAPPER}-1" in page
    assert "robothor_memory-20260901.sql.gz" in page
    assert box.state("heal_count") == "1"
    assert box.state("down_since") is None, "the down state survived a successful heal"


def test_a_heal_whose_every_step_worked_is_not_recovery_until_the_probe_says_so(box: Box):
    """The re-probe is the only thing that makes "auto-recovered" a fact.

    Every step of the heal can return 0 and leave the volume unusable — the
    reopened container mounts, and ext4 flips straight back to ``emergency_ro``
    because the bridge is still dropping writes. A guard that set ``healed=1``
    on the heal function's own exit status would page RECOVERED, clear
    ``down_since``, arm the quiet period, and go silent on an outage that never
    ended: the inert control, certified by its own log line. So the probe runs
    again afterwards and it, not the heal, decides.
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 1",  # every heal step works; the volume is still dead
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # The heal really did run — this is not a test of a refusal.
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), f"no heal ran:\n{box.argv}"
    assert box.ran(f"mount {box.mapper_dir / (MAPPER + '-1')} {box.mount}"), box.argv

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    page = box.pages[0]
    assert "BACKUP VOLUME DOWN" in page, page
    assert f"remapped as {MAPPER}-1, but the volume is still unusable" in page, page
    assert "auto-recovered" not in page, page
    assert box.state("heal_count") is None, "counted a drop that was never recovered"
    assert box.state("down_since"), "cleared the outage the volume is still in"


def test_every_heal_pages_so_a_flaky_bridge_is_never_masked(box: Box):
    """Two drops in one hour must be two pages. A dedup key shared with the
    first would let the sender's 1h cooldown swallow the second, and a bridge
    that flaps every 40 minutes would then look like a single fixed incident."""
    box.plug_in()
    box.stale_mapper()
    common = {
        "FAKE_CHECK_RCS": "1 0",
        "FAKE_DM_DEPS": "8:16",
        "FAKE_MAJMIN": "8:17",
        "FAKE_DM_OPEN": "1",
    }
    box.run(**common)
    box.stale_mapper()
    box.run(**common)
    assert len(box.pages) == 2, f"a second USB drop did not page: {box.pages}"
    assert "USB drop #2 since boot" in box.pages[1]
    keys = [page.splitlines()[0] for page in box.pages]
    assert keys[0] != keys[1], f"both heals paged under the same dedup key: {keys}"


def test_heal_disabled_pages_but_does_not_touch_the_device(box: Box):
    """The escape hatch has to be real: HEAL=0 leaves the disk alone."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(FAKE_CHECK_RCS="1", ROBOTHOR_VOLUME_GUARD_HEAL="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(box.pages) == 1
    assert "BACKUP VOLUME DOWN" in box.pages[0]
    assert box.ran("umount") == [], "unmounted with healing disabled"
    assert box.ran("cryptsetup") == []
    assert box.ran("mount") == []


def test_fsck_needing_manual_repair_never_mounts(box: Box):
    """``fsck.ext4 -p`` is preen-only: rc>=4 means the filesystem needs a
    human. Mounting it anyway, or re-running fsck without -p, risks turning a
    recoverable backup volume into an unrecoverable one."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_FSCK_RC="4",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("mount") == [], "mounted a filesystem that failed preen fsck"
    assert len(box.pages) == 1
    assert "manual fsck" in box.pages[0]
    # It must not be left open either — a half-healed mapping is what produced
    # the stale-mapper problem in the first place.
    assert box.ran(f"cryptsetup close {MAPPER}-1"), f"left the mapper open:\n{box.argv}"


def test_a_busy_mapper_is_never_fsckd_even_when_it_is_the_right_device(box: Box):
    """``umount -l`` RETURNS before the last reference is dropped.

    When the mapper's ``dmsetup deps`` still name the device on the bus, the
    guard reuses that mapping instead of opening a new one — and then it is
    about to ``fsck.ext4 -p`` a mapping the kernel is still handing out. That
    is how a volume that was merely degraded becomes a corrupted one. The open
    count has to be re-read at that moment, and a non-zero answer means stop:
    no fsck, no close, no mount.
    """
    box.plug_in()
    box.stale_mapper()  # a node that is NOT stale: same major:minor as the device
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_DM_DEPS="8:17",  # the mapping is backed by...
        FAKE_MAJMIN="8:17",  # ...the device that is on the bus: live, not stale
        FAKE_DM_OPEN="1",  # but something still holds it
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("fsck.ext4") == [], (
        f"ran fsck on a mapping with an opener — this corrupts a degraded "
        f"volume:\n{box.argv}"
    )
    assert box.ran("cryptsetup close") == [], f"closed a referenced mapping:\n{box.argv}"
    assert box.ran("mount") == [], f"mounted a mapping it had not repaired:\n{box.argv}"

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert f"mapper {MAPPER} still has 1 opener(s) after umount -l" in box.pages[0]
    assert "refusing to fsck a referenced mapping" in box.pages[0]


def test_a_free_live_mapper_is_healed_under_its_own_name(box: Box):
    """The control for the test above: same live mapping, open count 0. There
    is nothing to refuse — the heal proceeds, on the ORIGINAL name, because
    stacking a second mapping on a disk that already has a correct one is how
    the nine names get burned."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS="8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"mount {box.mapper_dir / MAPPER} {box.mount}"), (
        f"the live mapping was not remounted under its own name:\n{box.argv}"
    )
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1") == [], (
        f"burned a fresh mapper name on a mapping that was already correct:\n{box.argv}"
    )
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]
    assert f"remapped as {MAPPER})" in box.pages[0], box.pages[0]


@pytest.mark.parametrize("column3", ["none", "-", "/etc/robothor/does-not-exist.key"])
def test_without_a_usable_keyfile_the_guard_tears_nothing_down(box: Box, column3: str):
    """The heal's first act was to unmount and close; the reopen came later.

    If crypttab's third column is ``none``/``-``/unreadable, that reopen
    prompts on a console the timer does not have, fails, and the guard has
    converted a DEGRADED volume — wedged, but with its mapping intact — into
    an ABSENT one that nothing but a human can restore. The check belongs
    before the teardown, not after it.
    """
    box.plug_in()
    box.stale_mapper()
    box.no_keyfile(column3)
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_DM_DEPS="8:16",  # stale: a reopen is unavoidable
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("umount") == [], (
        f"unmounted a volume it could not put back:\n{box.argv}"
    )
    assert box.ran("cryptsetup close") == [], (
        f"closed a container it has no key to reopen:\n{box.argv}"
    )
    assert box.ran("cryptsetup open") == []
    assert box.ran("mount") == []

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert f"no non-interactive keyfile in crypttab (column 3 = {column3})" in box.pages[0]
    assert "refusing to tear down a mapping I cannot rebuild" in box.pages[0]
    assert "fix crypttab or reboot" in box.pages[0]


def test_a_live_mapping_is_remounted_rather_than_closed_and_reopened(box: Box):
    """The cheapest repair that can work, tried first.

    When the node already there is backed by the device on the bus and nothing
    holds it, the container does not need closing and the key does not need
    using: put the existing mapping back at its path. Closing and reopening it
    is strictly more that can go wrong, and it needs a key this path should not
    have to depend on.
    """
    box.plug_in()
    box.stale_mapper()
    box.no_keyfile()  # deliberately: this path must not need one
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS="8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("cryptsetup open") == [], f"reopened a live mapping:\n{box.argv}"
    assert box.ran("cryptsetup close") == [], f"closed a live mapping:\n{box.argv}"
    assert box.ran("fsck.ext4") == [], f"fsck'd a filesystem that only needed remounting:\n{box.argv}"
    assert box.ran(f"mount {box.mapper_dir / MAPPER} {box.mount}"), (
        f"never put the existing mapping back:\n{box.argv}"
    )
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]


def test_no_free_mapper_name_refuses_and_says_reboot(box: Box):
    """Nine stale mappings means the kernel references never went away. There
    is no tenth name to try; say what actually clears it."""
    box.plug_in()
    box.stale_mapper()
    for i in range(1, 10):
        (box.mapper_dir / f"{MAPPER}-{i}").write_text("")
    result = box.run(FAKE_CHECK_RCS="1", FAKE_DM_DEPS="8:16", FAKE_MAJMIN="8:17", FAKE_DM_OPEN="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("cryptsetup open") == [], "opened a tenth mapping"
    assert "9 stale mappings, reboot required" in box.pages[0]


def test_a_running_backup_unit_blocks_the_heal(box: Box):
    """Lazy-unmounting the volume out from under a running pg_basebackup would
    corrupt the very backup the guard exists to protect."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(FAKE_CHECK_RCS="1", FAKE_UNIT_STATE="activating")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("umount") == [], "unmounted while a backup unit was activating"
    assert box.ran("cryptsetup open") == []
    assert len(box.pages) == 1
    assert "activating" in box.pages[0]


# ── every way the heal can refuse or fail ────────────────────────────────────
#
# One test per failure seam. A heal has seven external steps and every one of
# them can fail; a suite that only ever drives the happy path is asserting that
# the guard works when nothing goes wrong, which is not the case it exists for.


def test_a_disk_smart_calls_failed_is_never_remounted(box: Box):
    """The firmware has given up on the disk. Mounting it again to keep taking
    backups onto it is worse than having no backup volume at all — and an fsck
    on a dying disk can finish the job."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_SMART_OUT="SMART Health Status: FAILED",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("cryptsetup open") == [], f"opened a dying disk:\n{box.argv}"
    assert box.ran("fsck.ext4") == [], f"fsck'd a dying disk:\n{box.argv}"
    assert box.ran("mount") == []
    assert box.ran("umount") == [], f"unmounted a disk it had already refused:\n{box.argv}"
    assert len(box.pages) == 1
    assert "SMART reports /dev/sdb as FAILED" in box.pages[0]


def test_a_device_that_is_not_a_luks_container_is_never_touched(box: Box):
    """``ROBOTHOR_VOLUME_GUARD_MAPPER`` and the crypttab can be wrong, and a
    by-uuid path can be reused by a different disk. The guard refuses anything
    it cannot identify as its own container — fsck against the wrong device is
    unrecoverable."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(FAKE_CHECK_RCS="1", FAKE_ISLUKS_RC="1", FAKE_DM_DEPS="8:16", FAKE_MAJMIN="8:17")
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("cryptsetup open") == []
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []
    assert box.ran("umount") == []
    assert len(box.pages) == 1
    assert "is not a LUKS container" in box.pages[0]


def test_a_failed_open_stops_before_the_fsck(box: Box):
    """A wrong or unreadable key, or a container the kernel will not map. The
    mapper path would not exist; ``fsck.ext4`` against it would either do
    nothing or find something else there."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_OPEN_RC="1",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), f"never tried:\n{box.argv}"
    assert box.ran("fsck.ext4") == [], f"fsck'd a mapping that was never opened:\n{box.argv}"
    assert box.ran("mount") == []
    assert len(box.pages) == 1
    assert "cryptsetup open failed" in box.pages[0]


def test_a_failed_mount_closes_the_mapping_this_run_opened(box: Box):
    """A fresh mapper node left behind by every failed tick is a name burned,
    and nine burned names is a reboot. The guard must not manufacture the
    condition it exists to recover from — and it must not page 'recovered'."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_RC="1",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"mount {box.mapper_dir / (MAPPER + '-1')} {box.mount}"), box.argv
    assert box.ran(f"cryptsetup close {MAPPER}-1"), (
        f"left the mapping it had just opened behind:\n{box.argv}"
    )
    assert len(box.pages) == 1
    page = box.pages[0]
    assert "auto-recovered" not in page, f"paged a recovery that did not happen: {page}"
    assert "BACKUP VOLUME DOWN" in page
    assert f"mount {box.mapper_dir / (MAPPER + '-1')} at {box.mount} failed" in page


def test_a_failed_umount_stops_before_anything_is_closed(box: Box):
    """If the volume cannot be released, everything after it is being done to a
    mount that is still live. Stop at the first step."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_UMOUNT_RC="1",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",  # otherwise closeable: the umount is what stops it
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("umount"), box.argv
    assert box.ran("cryptsetup close") == [], f"closed under a live mount:\n{box.argv}"
    assert box.ran("cryptsetup open") == []
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []
    assert len(box.pages) == 1
    assert f"umount -l {box.mount} failed" in box.pages[0]


def test_a_foreign_filesystem_at_the_mountpoint_is_never_unmounted(box: Box):
    """``umount -l <path>`` names a PATH, and the guard is root.

    The mountpoint is a directory anyone with root can mount over: a rescue
    image, a rsync staging tree, another disk mounted there while the real one
    was away. If that happens the probe fails (it is not the backup volume) and
    the guard's first side effect would be to lazily unmount somebody else's
    filesystem out from under whatever is using it. Identify what is actually
    mounted there and refuse anything that is not this guard's own mapper.
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_SOURCE="/dev/sda1",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("umount") == [], f"unmounted a filesystem that is not ours:\n{box.argv}"
    assert box.ran("cryptsetup open") == []
    assert box.ran("cryptsetup close") == []
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert (
        f"something other than the backup mapper is mounted at {box.mount} "
        f"(/dev/sda1) — refusing to unmount it" in box.pages[0]
    ), box.pages[0]


def test_a_findmnt_that_could_not_answer_does_not_read_as_nothing_mounted(box: Box):
    """"I could not ask" is not an answer of "nothing".

    findmnt exits non-zero for a mountpoint with nothing on it AND for a real
    failure — a missing binary, a hung /proc/self/mountinfo read. Folding both
    into "nothing is mounted there" means the heal walks past the one gate that
    tells it whose filesystem is at the path, and then closes and reopens the
    container underneath whatever really was. Refuse the tick instead: the
    volume is already down, and the next one costs ten minutes.
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNTED_RC="127",  # findmnt is not there at all
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("umount") == [], f"unmounted on a guess:\n{box.argv}"
    assert box.ran("cryptsetup close") == [], f"closed on a guess:\n{box.argv}"
    assert box.ran("cryptsetup open") == [], f"reopened on a guess:\n{box.argv}"
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert f"could not ask findmnt what is mounted at {box.mount}" in box.pages[0], box.pages[0]
    assert "refusing to guess" in box.pages[0], box.pages[0]


def test_two_filesystems_stacked_at_the_mountpoint_are_never_unmounted(box: Box):
    """A mountpoint can hold a STACK, and findmnt then prints a row each.

    Taking the first row and unmounting the path is aimed at the LAST one —
    ``umount`` pops the top of the stack, which is the row findmnt printed
    last. So the guard would check the identity of one filesystem and detach a
    different one, and the check that exists precisely to stop that would have
    signed it off. Nothing here can tell which of them belongs to this guard,
    so none of them is touched.
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        # ours, with somebody's rescue image mounted over the top of it
        FAKE_MOUNT_SOURCE=f"{box.mapper_dir / MAPPER}\n/dev/sda1",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("umount") == [], f"unmounted one of a stack:\n{box.argv}"
    assert box.ran("cryptsetup close") == []
    assert box.ran("cryptsetup open") == []
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert f"2 filesystems stacked at {box.mount}" in box.pages[0], box.pages[0]


@pytest.mark.parametrize(
    "source",
    [
        # our name is a PREFIX of it, which makes it a different mapping, not ours
        f"{MAPPER}x",
        f"{MAPPER}-1-other",
        # a suffix is one name component: no escaping back out of /dev/mapper
        f"{MAPPER}-1/../../sda1",
    ],
)
def test_a_mapping_that_merely_starts_with_our_name_is_not_ours(box: Box, source: str):
    """``robothor-backup`` being a prefix of a name does not make that name this
    guard's mapper. The suffix has to be one ``-<token>`` component."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_SOURCE=str(box.mapper_dir / source),
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("umount") == [], f"unmounted {source}:\n{box.argv}"
    assert "refusing to unmount it" in box.pages[0]


def test_a_mapper_wearing_our_name_but_backed_by_another_device_is_not_ours(box: Box):
    """The lexical check is necessary and not sufficient.

    ``robothor-backup-<token>`` is a NAME, and a name is not an identity: any
    device-mapper node can be created under it, and the guard would then
    lazy-unmount somebody else's filesystem because it liked the spelling. What
    makes a node ours is the DEVICE — either ``dmsetup deps`` resolving to the
    same major:minor the crypttab UUID resolves to, or a dm-crypt UUID naming
    our own LUKS container (that is our corpse after a drop, which is exactly
    the thing the heal has to unmount). ``robothor-backup-evil`` is neither.
    """
    box.plug_in()
    box.stale_mapper()
    evil = box.mapper_dir / f"{MAPPER}-evil"
    box.mapper_node(evil.name)
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_SOURCE=str(evil),
        FAKE_DM_DEPS="8:99",  # backed by a device that is not ours
        FAKE_DM_UUID="CRYPT-LUKS2-00000000000000000000000000000000-not-ours",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("umount") == [], f"lazy-unmounted a stranger's filesystem:\n{box.argv}"
    assert box.ran("cryptsetup open") == []
    assert box.ran("cryptsetup close") == []
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []

    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert (
        f"something other than the backup mapper is mounted at {box.mount} "
        f"({evil} is not backed by {box.device}) — refusing to unmount it"
    ) in box.pages[0], box.pages[0]


def test_our_uuid_in_a_strangers_node_NAME_does_not_make_the_node_ours(box: Box):
    """A dm-crypt UUID is ``CRYPT-LUKS<n>-<container uuid>-<node name>``, and
    only the FIRST field is an identity — the second is a string somebody
    chose.

    Comparing with the dashes stripped out of the whole thing and a substring
    test let our UUID match wherever it appeared, the name half included. So a
    node called ``robothor-backup-<our uuid in hex>`` over somebody else's LUKS
    container passed as ours, and the guard would lazy-unmount and fsck it —
    the same "a name is not an identity" hole the deps check was added to
    close, reopened inside the check that replaced it. Anchor the comparison
    to the field that means something.
    """
    box.plug_in()
    box.stale_mapper()
    hexuuid = UUID.replace("-", "")
    impostor = box.mapper_dir / f"{MAPPER}-{hexuuid}"
    box.mapper_node(impostor.name)
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_SOURCE=str(impostor),
        FAKE_DM_DEPS="8:99",  # backed by a device that is not ours
        # a STRANGER's container, wearing our UUID only in the name field
        FAKE_DM_UUID=f"CRYPT-LUKS2-00000000000000000000000000000000-{MAPPER}-{hexuuid}",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("umount") == [], f"unmounted a stranger's container:\n{box.argv}"
    assert box.ran("cryptsetup open") == []
    assert box.ran("cryptsetup close") == []
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []
    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert "refusing to unmount it" in box.pages[0], box.pages[0]


def test_our_own_corpse_with_no_deps_at_all_is_still_ours_to_unmount(box: Box):
    """The control for the refusal above, and the case identity must not break.

    On 2026-08-27 the wedged node was an orphaned ``error`` target: the device
    had gone, so ``dmsetup deps`` named NOTHING. An identity check that only
    compared deps would have refused to unmount the very thing the recovery
    unmounts — inert on exactly the signature it was written for. The dm-crypt
    UUID still names our LUKS container, so the node is still ours.
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS="",  # an error target depends on nothing
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",  # held by the kernel, as it was on the night
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"umount -l {box.mount}"), f"refused its own corpse:\n{box.argv}"
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), box.argv
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]


def test_a_path_spec_crypttab_still_knows_its_own_corpse(box: Box):
    """The corpse case must not depend on how crypttab spells the device.

    ``robothor-backup UUID=<…>`` hands the container's UUID over for free, and
    the check that recognises this guard's own wedged node after the device has
    dropped off — when ``dmsetup deps`` has nothing left to say — is built on
    it. Spell the same device as ``/dev/disk/by-id/…`` and that UUID is simply
    absent, the check silently answers "not ours", and the guard refuses to
    unmount the very node the recovery exists to unmount. The header has the
    UUID; ask it, read-only.
    """
    box.plug_in()
    box.stale_mapper()
    box.crypttab_names_a_path()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS="",  # an error target depends on nothing
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",  # held by the kernel, as it was on the night
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran(f"cryptsetup luksUUID {box.device}"), (
        f"never asked the header for the container UUID:\n{box.argv}"
    )
    assert box.ran(f"umount -l {box.mount}"), f"refused its own corpse:\n{box.argv}"
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), box.argv
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]


def test_an_unreadable_luks_header_says_identity_is_degraded(box: Box):
    """And when the header cannot be read, say so rather than carrying on as
    if the UUID were simply absent. Identity falls back to ``deps`` alone,
    which is the check that cannot see a corpse — a fact the operator reading
    the journal after a refusal needs in front of them."""
    box.plug_in()
    box.stale_mapper()
    box.crypttab_names_a_path()
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_LUKSUUID_RC="1",
        FAKE_DM_DEPS="",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "identity is degraded to deps-only" in result.stdout, result.stdout
    assert box.ran("umount") == [], f"unmounted a node it could not identify:\n{box.argv}"


@pytest.mark.parametrize("suffix", ["-1", "-9", "-b"])
def test_the_mapper_from_a_previous_heal_is_the_one_reused(box: Box, suffix: str):
    """The control for the refusal above, and the case it must not break.

    After one heal the mount's source is ``<name>-1``, not ``<name>`` — and a
    volume recovered BY HAND wears whatever name the operator picked at 3am.
    The box this guard ships to is mounted from ``/dev/mapper/robothor-backup-b``
    right now, from the 2026-08-27 recovery. A check that only accepted the
    base name, or only ``-[1-9]``, would refuse to heal the very box it was
    written for — inert on arrival, and only discoverable during an outage.

    And unmounting it is not enough: the mapping under that name is the
    device's OWN, so it is the one to put back. Looking only at the bare
    ``robothor-backup`` — a corpse here — meant opening a SECOND LUKS mapping
    over a disk that already had a live one and abandoning the live node. So
    this asserts which path ran, not merely that the unmount happened.
    """
    box.plug_in()
    box.stale_mapper()  # the bare name exists and is NOT the live mapping
    box.mapper_node(f"{MAPPER}{suffix}")
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_MOUNT_SOURCE=str(box.mapper_dir / f"{MAPPER}{suffix}"),
        FAKE_DM_DEPS_MAP=f"{MAPPER}=8:16 {MAPPER}{suffix}=8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"umount -l {box.mount}"), f"refused its own mapper:\n{box.argv}"
    assert box.ran(f"mount {box.mapper_dir / (MAPPER + suffix)} {box.mount}"), (
        f"did not put the live mapping back under its own name:\n{box.argv}"
    )
    assert box.ran("cryptsetup open") == [], (
        f"opened a second mapping over a device that already had a live one:"
        f"\n{box.argv}"
    )
    assert box.ran("cryptsetup close") == [], f"closed a mapping it only borrowed:\n{box.argv}"
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]
    assert f"remapped as {MAPPER}{suffix})" in box.pages[0], box.pages[0]


def test_a_live_mapper_under_another_name_is_reused_rather_than_stacked(box: Box):
    """Nothing is mounted from the live node — but it is still the live node.

    The bare ``robothor-backup`` at the mountpoint is a corpse (its deps do not
    name the device) while a previous heal's ``robothor-backup-1`` IS backed by
    the device that came back. Deciding by name, the guard saw only the bare
    node, called it stale, and opened a THIRD LUKS mapping over a disk that
    already had a live one — burning a name and abandoning the node it could
    simply have mounted. The reuse question is "which node is backed by the
    device", and the answer needs no key.
    """
    box.plug_in()
    box.stale_mapper()
    box.mapper_node(f"{MAPPER}-1")
    box.no_keyfile()  # a reuse needs no reopen, so it must not need a key
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS_MAP=f"{MAPPER}=8:16 {MAPPER}-1=8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"umount -l {box.mount}"), f"never released the corpse:\n{box.argv}"
    assert box.ran(f"mount {box.mapper_dir / (MAPPER + '-1')} {box.mount}"), (
        f"never mounted the mapping that was already the device's own:\n{box.argv}"
    )
    assert box.ran("cryptsetup open") == [], (
        f"stacked a second mapping on a device that already had a live one:\n{box.argv}"
    )
    assert len(box.pages) == 1
    assert f"remapped as {MAPPER}-1)" in box.pages[0], box.pages[0]


def test_a_held_live_mapper_is_refused_rather_than_stacked(box: Box):
    """The same question, answered "yes, and something holds it".

    A live mapping with an opener is still a live mapping: opening a second
    LUKS mapping over that disk is exactly what must never happen. The guard
    stops and says who to look for, and the next tick mounts it once the holder
    lets go.
    """
    box.plug_in()
    box.stale_mapper()
    box.mapper_node(f"{MAPPER}-1")
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_DM_DEPS_MAP=f"{MAPPER}=8:16 {MAPPER}-1=8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("cryptsetup open") == [], (
        f"stacked a mapping over a device whose live one was merely busy:\n{box.argv}"
    )
    assert box.ran("fsck.ext4") == []
    assert box.ran("mount") == []
    assert len(box.pages) == 1
    assert f"mapper {MAPPER}-1 still has 1 opener(s) after umount -l" in box.pages[0]


def test_a_mapping_this_run_opened_is_never_second_guessed_by_an_open_count(box: Box):
    """The pre-fsck gate is about mappings the guard did NOT open.

    A container this run opened under a free name is the guard's alone: nothing
    else has had the chance to reference it, and an open count read afterwards
    that says otherwise is the kernel's bookkeeping, not a user. Keying the gate
    on the NAME (``MAPPER_USED == MAPPER_BASE``) refused the heal here — a heal
    whose every step had just succeeded — and left the volume unmounted with a
    page blaming an opener that does not exist. The invariant is "did I open
    this myself", not "what is it called".
    """
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_DM_DEPS="8:16",  # stale: the node is closed and reopened
        FAKE_MAJMIN="8:17",
        # free when the stale node is closed, "held" when asked again after the
        # guard has opened its own container under the freed name.
        FAKE_DM_OPEN="0 1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"cryptsetup close {MAPPER}"), f"never closed the stale node:\n{box.argv}"
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}"), (
        f"never reopened under the freed name:\n{box.argv}"
    )
    assert box.ran(f"fsck.ext4 -p {box.mapper_dir / MAPPER}"), (
        f"refused to fsck a mapping it had just opened itself:\n{box.argv}"
    )
    assert box.ran(f"mount {box.mapper_dir / MAPPER} {box.mount}"), box.argv
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]


def test_a_reused_mapping_that_becomes_referenced_before_the_fsck_is_refused(box: Box):
    """The other half of that gate, and the half that has teeth.

    The mapping the guard reuses is one it did not open, so between the check at
    the unmount and the fsck a new opener can appear. It is re-read at the last
    possible moment and a non-zero answer stops the heal — no fsck, no mount.
    """
    box.plug_in()
    box.stale_mapper()
    box.no_keyfile()  # the reuse path must not need one
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_RC="1",  # the cheap remount fails: fall through to the fsck
        FAKE_DM_DEPS="8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0 1",  # free at the unmount, referenced at the fsck
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("fsck.ext4") == [], (
        f"fsck'd a mapping that acquired an opener after the unmount:\n{box.argv}"
    )
    assert box.ran("cryptsetup open") == []
    assert len(box.pages) == 1
    assert "refusing to fsck a referenced mapping" in box.pages[0]


def test_the_busy_mapper_page_says_the_volume_is_now_unmounted(box: Box):
    """This refusal happens AFTER the lazy unmount, so the operator is not
    reading about a degraded volume — they are reading about a volume that is
    now gone from the path until the holder lets go. A page that stops at
    "refusing to fsck" reads as "nothing changed", and the one action that
    matters (find the holder, stop it) looks optional."""
    box.plug_in()
    box.stale_mapper()
    box.run(
        FAKE_CHECK_RCS="1",
        FAKE_DM_DEPS="8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    page = box.pages[0]
    assert "the volume is now UNMOUNTED" in page, page
    assert "the next tick remounts it once the holder lets go" in page, page


def test_a_reused_mapping_is_never_closed_when_the_repaired_mount_fails(box: Box):
    """The failure cleanup closes what THIS run opened. A mapping it merely
    reused was somebody else's before the heal and stays theirs after it:
    closing it would destroy the one thing the next tick can still remount, and
    would burn the name while it is at it."""
    box.plug_in()
    box.stale_mapper()
    box.no_keyfile()  # the reuse path must not need one
    result = box.run(
        FAKE_CHECK_RCS="1",
        FAKE_MOUNT_RC="1 1",  # the cheap remount AND the repaired one both fail
        FAKE_DM_DEPS="8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(box.ran(f"mount {box.mapper_dir / MAPPER} {box.mount}")) == 2, box.argv
    assert box.ran("cryptsetup close") == [], (
        f"closed a mapping it had only borrowed:\n{box.argv}"
    )
    assert len(box.pages) == 1
    assert "auto-recovered" not in box.pages[0]
    assert f"mount {box.mapper_dir / MAPPER} at {box.mount} failed" in box.pages[0]


def test_a_close_that_fails_is_not_fatal_the_new_name_is_the_point(box: Box):
    """The stale node usually CANNOT be closed — that kernel reference is the
    entire reason the container is reopened under ``<name>-1``. A failed close
    must therefore not abort the heal."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_CLOSE_RC="1",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",  # looks closeable; the close fails anyway
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"cryptsetup close {MAPPER}"), f"never tried the close:\n{box.argv}"
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), (
        f"a failed close aborted the heal:\n{box.argv}"
    )
    assert len(box.pages) == 1
    assert f"remapped as {MAPPER}-1" in box.pages[0]


def test_a_stranger_wearing_our_bare_name_is_never_closed(box: Box):
    """``cryptsetup close`` destroys a mapping, and the name it is given here
    is a FALLBACK, not an identity.

    When nothing is mounted at the path the guard closes ``${MAPPER_BASE}``
    simply because that is what its own node is usually called. Every other
    node this script touches has been through ``node_is_ours`` first; this one
    had not, so a stranger's mapping parked under the bare name — backed by
    another device, wearing another container's UUID — was torn down by a
    guard that liked the spelling. Leave it alone and take the next free name.
    """
    box.plug_in()
    box.stale_mapper()  # a node exists under the bare name; it is not ours
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_MOUNTED_RC="1",  # nothing is mounted at the path
        FAKE_DM_DEPS="8:99",  # backed by a device that is not ours
        FAKE_DM_UUID="CRYPT-LUKS2-00000000000000000000000000000000-not-ours",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",  # free, so nothing BUT identity can stop the close
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert box.ran("cryptsetup close") == [], (
        f"closed a mapping that is not ours:\n{box.argv}"
    )
    assert box.ran(f"cryptsetup open {box.device} {MAPPER}-1"), (
        f"did not step over the stranger onto the first free name:\n{box.argv}"
    )
    assert len(box.pages) == 1, f"expected exactly one page, got {box.pages}"
    assert "auto-recovered" in box.pages[0]
    assert f"remapped as {MAPPER}-1" in box.pages[0]


def test_a_live_mapping_that_will_not_remount_is_repaired_under_its_own_name(box: Box):
    """The fall-back from the cheap path. The node is the device's own and
    free, but the plain remount fails — ext4 needs the preen first. The
    container is still not reopened: the mapping is already correct, so it is
    fsck'd and mounted under the ORIGINAL name rather than stacking a second
    mapping on the same disk."""
    box.plug_in()
    box.stale_mapper()
    box.no_keyfile()  # this path must not need a key
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_MOUNT_RC="1 0",  # the cheap remount fails, the repaired one works
        FAKE_DM_DEPS="8:17",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="0",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("cryptsetup open") == [], f"reopened a live mapping:\n{box.argv}"
    assert box.ran(f"fsck.ext4 -p {box.mapper_dir / MAPPER}"), f"never repaired it:\n{box.argv}"
    assert len(box.ran(f"mount {box.mapper_dir / MAPPER} {box.mount}")) == 2, (
        f"expected the cheap remount and then the repaired one:\n{box.argv}"
    )
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]
    assert f"remapped as {MAPPER})" in box.pages[0]


# The five units that write to the backup volume. This list is the deferral
# contract: each one of them, mid-run, must stop the heal on its own, because
# `umount -l` under any of them corrupts the backup the guard exists to
# protect. It is pinned here member by member — a unit quietly dropped from
# BACKUP_UNITS is invisible to a test that puts the whole fleet in the same
# state, and would only show up as a corrupt backup discovered at restore time.
DEFERRING_UNITS = [
    "robothor-backup-local.service",
    "robothor-backup-offsite.service",
    "robothor-backup-verify.service",
    "robothor-basebackup.service",
    "robothor-wal-offsite.service",
]


@pytest.mark.parametrize("unit", DEFERRING_UNITS)
def test_each_backup_unit_defers_the_heal_on_its_own(box: Box, unit: str):
    box.plug_in()
    box.stale_mapper()
    result = box.run(FAKE_CHECK_RCS="1", FAKE_ACTIVATING=unit)
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran("umount") == [], f"unmounted while {unit} was mid-run:\n{box.argv}"
    assert box.ran("cryptsetup open") == []
    assert box.ran("mount") == []
    assert len(box.pages) == 1
    assert f"heal deferred: {unit} is activating" in box.pages[0], box.pages[0]


def test_a_unit_that_does_not_write_to_the_volume_does_not_defer_the_heal(box: Box):
    """The list is not "anything that happens to be running". Deferring on a
    unit that never touches the volume would postpone the heal indefinitely on
    a busy box — the failure mode is a guard that never fires."""
    box.plug_in()
    box.stale_mapper()
    result = box.run(
        FAKE_CHECK_RCS="1 0",
        FAKE_ACTIVATING="robothor-engine.service",
        FAKE_DM_DEPS="8:16",
        FAKE_MAJMIN="8:17",
        FAKE_DM_OPEN="1",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.ran(f"umount -l {box.mount}"), f"the heal never ran:\n{box.argv}"
    assert len(box.pages) == 1
    assert "auto-recovered" in box.pages[0]


# ── back to healthy ──────────────────────────────────────────────────────────


def test_recovery_is_announced_once_and_clears_the_state(box: Box):
    """The volume came back without the guard (device replugged, operator
    fixed it). Say so once, then re-arm — otherwise the next outage is
    suppressed by a day-old last_paged stamp."""
    box.run(FAKE_CHECK_RCS="1")
    assert len(box.pages) == 1
    result = box.run(FAKE_CHECK_RCS="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(box.pages) == 2, f"recovery was not announced: {box.pages}"
    assert "healthy again" in box.pages[1]
    assert box.state("down_since") is None
    assert box.state("last_paged") is None

    third = box.run(FAKE_CHECK_RCS="0")
    assert third.returncode == 0, third.stdout + third.stderr
    assert len(box.pages) == 2, "kept announcing a recovery that already happened"


# ── the sender failing is itself news ────────────────────────────────────────


def test_undelivered_page_fails_the_guard_and_does_not_arm_the_stamp(box: Box):
    """``liveness_probe.sh`` checks the sender's exit status rather than
    assuming a page was delivered; so does this. Exit 1 makes the unit fail,
    which fires its own OnFailure=. Arming last_paged on an undelivered page
    would suppress the retry for a day."""
    result = box.run(FAKE_CHECK_RCS="1", FAKE_ALERT_RC="1")
    assert result.returncode == 1, (
        f"the guard exited {result.returncode} on an UNDELIVERED page — its "
        f"OnFailure= hook never fires and the outage is silent\n{result.stderr}"
    )
    assert box.state("last_paged") is None, "armed the repage stamp on a page nobody received"


# ── units ────────────────────────────────────────────────────────────────────


def _directives(path: Path) -> str:
    """Unit content minus comments — a comment may legitimately DISCUSS a
    directive it deliberately omits."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith(("#", ";"))
    )


def test_timer_probes_from_boot_and_does_not_catch_up(box: Box):
    src = _directives(TIMER)
    assert "OnBootSec=3min" in src, (
        "a box that boots with the volume already wedged is never probed"
    )
    assert "OnUnitActiveSec=10min" in src
    assert "Persistent=" not in src, (
        "a catch-up run for every 10-minute tick the box was off would page "
        "about an outage that is already over"
    )


def test_service_does_not_sandbox_itself_out_of_the_mount_namespace(box: Box):
    """The guard mounts into the HOST namespace. PrivateMounts/PrivateTmp give
    the unit its own namespace, so the mount it performs would be invisible to
    every other unit — the guard would report success and the backups would
    still find nothing."""
    src = _directives(SERVICE)
    directives = [line.strip() for line in src.splitlines() if line.strip()]
    for banned in ("PrivateTmp=", "PrivateMounts=", "ProtectSystem=", "ProtectHome="):
        offending = [d for d in directives if d.startswith(banned)]
        assert not offending, (
            f"{SERVICE.name} sets {offending} — the mount would land in a private "
            "namespace and no other unit would ever see it"
        )
    assert "OnFailure=robothor-alert@%n.service" in src, (
        "a guard whose own page failed must itself be paged about"
    )
    assert "TimeoutStartSec=900" in src, "fsck on a large volume outlives the 90s default"


def test_tmpfiles_conf_creates_the_marker_directory(box: Box):
    """The markers live on NVMe: the disk that breaks must not be the disk
    holding the evidence of when it last worked."""
    rows = [
        line.split()
        for line in TMPFILES.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert rows, f"{TMPFILES.name} has no tmpfiles row"
    fields = rows[0]
    assert fields[0] == "d"
    assert fields[1] == "/var/lib/robothor/backup-state"
    assert fields[2] == "2775"


def test_the_guard_is_shellcheck_clean_and_parses():
    assert subprocess.run(["bash", "-n", str(GUARD)], timeout=30).returncode == 0
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(GUARD)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
