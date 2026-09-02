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
        self.alert_log = tmp_path / "alert.log"

        self._install_stubs()

    # -- stubs -----------------------------------------------------------
    def _stub(self, name: str, body: str) -> None:
        _script(
            self.bin / name,
            f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "{self.log}"\n{body}\n',
        )

    def _install_stubs(self) -> None:
        self._stub("findmnt", 'exit "${FAKE_MOUNTED_RC:-0}"')
        self._stub("umount", 'exit "${FAKE_UMOUNT_RC:-0}"')
        self._stub("mount", 'exit "${FAKE_MOUNT_RC:-0}"')
        self._stub("fsck.ext4", 'exit "${FAKE_FSCK_RC:-0}"')
        self._stub("smartctl", 'printf "%s\\n" "${FAKE_SMART_OUT:-SMART Health Status: OK}"')
        self._stub("systemctl", 'printf "%s\\n" "${FAKE_UNIT_STATE:-inactive}"')
        self._stub(
            "dmsetup",
            'case "$1" in\n'
            '  info) printf "%s\\n" "${FAKE_DM_OPEN:-0}" ;;\n'
            '  deps) printf "1 dependencies : (%s)\\n" "${FAKE_DM_DEPS:-8:16}" ;;\n'
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
        self._stub(
            "cryptsetup",
            'case "$1" in\n'
            '  isLuks) exit "${FAKE_ISLUKS_RC:-0}" ;;\n'
            '  open)   rc="${FAKE_OPEN_RC:-0}"\n'
            '          [ "$rc" = 0 ] && : > "$FAKE_MAPPER_DIR/$3"\n'
            '          exit "$rc" ;;\n'
            '  close)  rm -f "$FAKE_MAPPER_DIR/$2"; exit "${FAKE_CLOSE_RC:-0}" ;;\n'
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
                # still be unable to deliver anything.
                "ROBOTHOR_ALERT_SUPPRESS": "1",
                "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
            }
        )
        env.update(extra)
        return env

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        # FAKE_CHECK_RCS describes ONE guard run (probe, then post-heal
        # re-probe), so the invocation counter resets per run.
        self.check_count.unlink(missing_ok=True)
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
    assert "Still running: WAL offsite" in page
    assert "PITR RPO intact, dump-tier RPO growing" in page
    assert "Runbook: BACKUP_VOLUME_GUARD.md" in page
    # The last-good facts come from the NVMe markers, never from the volume
    # that just failed.
    assert "robothor_memory-20260901.sql.gz" in page
    assert "00000001000000A2000000F3" in page


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
