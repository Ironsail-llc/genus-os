"""systemd + pager hardening: the OnFailure path must survive the boot window.

Two confirmed pages were lost forever on the 2026-08-19 boot:

- robothor-backup-offsite failed before DNS was up; the pager's single curl
  died on "Could not resolve host: api.telegram.org" and systemd fires
  OnFailure exactly once — the page was gone.
- robothor-orchestrator failed before the secrets were decrypted; the pager
  exited "ROBOTHOR_TELEGRAM_BOT_TOKEN is not set" — same result.

Fixes under test here:

- scripts/send_failure_alert.sh retries the send in a bounded loop and
  re-sources the secrets inside the loop, so a page raised during the
  boot-DNS/secrets window is delivered once the box comes up.
- infra/systemd/robothor-alert@.service restarts itself on failure so even
  a fully exhausted pager run gets systemd-level retries.
- Backup-chain units declare RequiresMountsFor=/mnt/robothor-backup and
  network-online ordering so Persistent=true catch-up runs stop racing the
  mount and DNS at boot.
- scripts/cron-wrapper.sh pages on a non-zero exit of the wrapped command
  (fail-open: a broken pager must not change the cron job's exit code).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SEND = REPO_ROOT / "scripts" / "send_failure_alert.sh"
WRAPPER = REPO_ROOT / "scripts" / "cron-wrapper.sh"
VOLUME_CHECK = REPO_ROOT / "scripts" / "backup-volume-check.sh"
STATE_LIB = REPO_ROOT / "scripts" / "backup-state.sh"
VOLUME_GUARD = REPO_ROOT / "scripts" / "backup-volume-guard.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

# The backup units whose ExecCondition= must consult the volume probe, the
# access mode each of them actually needs, and the path each one is allowed to
# gate on. wal-offsite is deliberately absent — see
# TestAWedgedVolumeSkipsTheBackupUnits.
VOLUME_GATED_UNITS = {
    "robothor-backup-local.service": ("--rw", "/mnt/robothor-backup"),
    "robothor-basebackup.service": ("--rw", "/mnt/robothor-backup/robothor"),
    "robothor-backup-offsite.service": ("--ro", "/mnt/robothor-backup/robothor/db"),
    "robothor-backup-verify.service": ("--ro", "/mnt/robothor-backup/robothor/db"),
}

# The script each unit's ExecStart= runs. A unit must never gate on a path that
# only its OWN ExecStart= creates.
UNIT_EXEC_SCRIPT = {
    "robothor-backup-local.service": "backup-ssd.sh",
    "robothor-basebackup.service": "pg-basebackup.sh",
    "robothor-backup-offsite.service": "backup-offsite.sh",
    "robothor-backup-verify.service": "backup-offsite.sh",
}

# What actually brings each gatable path into existence. None = the mount.
PATH_CREATED_BY = {
    "/mnt/robothor-backup": None,
    "/mnt/robothor-backup/robothor": "backup-ssd.sh",
    "/mnt/robothor-backup/robothor/basebackup": "pg-basebackup.sh",
    "/mnt/robothor-backup/robothor/db": "backup-ssd.sh",
}


def volume_condition_line(unit: str) -> str:
    return next(
        line
        for line in unit_text(unit).splitlines()
        if line.startswith("ExecCondition=") and "backup-volume-check.sh" in line
    )


def volume_gate_path(unit: str) -> str:
    return volume_condition_line(unit).split()[-1]

FAKE_TOKEN_ENV = {
    "ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123",
    "ROBOTHOR_TELEGRAM_CHAT_ID": "42",
}

# A base URL that cannot be reached and cannot be mistaken for the real API.
STUB_API_BASE = "http://127.0.0.1:1"


# ── fake curl helpers ────────────────────────────────────────────────────────


def install_fake_curl(tmp_path: Path, fail_first: int = 0) -> Path:
    """Install a curl stand-in on PATH that records argv per call.

    Fails (exit 1) for the first ``fail_first`` invocations, then succeeds —
    the boot-DNS window in miniature. Returns the argv log path.
    """
    log = tmp_path / "curl-args.txt"
    count = tmp_path / "curl-count.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        f'n=$(cat "{count}" 2>/dev/null || echo 0)\n'
        f'echo $((n + 1)) > "{count}"\n'
        f"[ $((n + 1)) -le {fail_first} ] && exit 1\n"
        # Real curl with -w '%{http_code}' ALWAYS prints a status. A double that
        # stays silent lets a caller which checks the status look broken, and
        # lets one which ignores it look correct -- which is how the HTTP-401
        # blind spot survived. Emit 200 on the success path.
        "for a in \"$@\"; do [ \"$a\" = '%{http_code}' ] && printf '200'; done\n"
        "exit 0\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def curl_calls(log: Path) -> int:
    """Each invocation records exactly one .../sendMessage URL argument."""
    if not log.exists():
        return 0
    return log.read_text().count("/sendMessage")


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        # The script SETS its PATH (a root unit must not inherit the operator's
        # user-writable directories), so the stub directory reaches it through the
        # one documented seam — see infra/systemd/README.md.
        "ROBOTHOR_EXTRA_PATH": str(tmp_path / "bin"),
        "HOME": str(tmp_path),
        # Never touch the real /run/robothor state or secrets from a test.
        "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
        # The fallback state dir is a real, shared path (/tmp/...-$uid) that
        # survives between test runs — pin it per test or one run's stamp
        # silently suppresses the next run's page.
        "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(tmp_path / "alert-cooldown-fallback"),
        "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
        "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
        # The spool is DURABLE (/var/lib, survives reboot) and is drained by
        # every later send and every liveness tick. A test that spools into
        # the real directory therefore hands the operator a page composed of
        # fixture text minutes later — the 2026-08-27 accident with a longer
        # fuse. Pin it per test.
        "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
        # Delivery is neutralised by the fake curl on PATH — but only for the
        # tests that install one. A case that forgets it (or an edit that
        # stops installing it) would POST a fixture page to the real API with
        # whatever token was in scope, so the base URL is pinned as well:
        # two independent seams, because the one that is easy to forget is
        # the one that pages.
        "ROBOTHOR_TELEGRAM_API_BASE": STUB_API_BASE,
        # Fast by default; individual tests override.
        "ROBOTHOR_ALERT_RETRY_DELAY": "0",
        "ROBOTHOR_ALERT_MAX_ATTEMPTS": "1",
    }
    env.update(extra)
    return env


def run_send(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SEND), "robothor-sample.service"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


# ── send_failure_alert.sh: bounded retry loop ────────────────────────────────


class TestSendRetriesThroughTheBootWindow:
    def test_retries_until_the_send_succeeds(self, tmp_path: Path):
        """DNS dead for the first two attempts, then up — the page must land."""
        log = install_fake_curl(tmp_path, fail_first=2)
        env = base_env(tmp_path, ROBOTHOR_ALERT_MAX_ATTEMPTS="10", **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 3

    def test_success_after_retry_writes_the_cooldown_stamp(self, tmp_path: Path):
        install_fake_curl(tmp_path, fail_first=1)
        env = base_env(tmp_path, ROBOTHOR_ALERT_MAX_ATTEMPTS="5", **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        state_dir = tmp_path / "alert-cooldown"
        assert state_dir.exists() and any(state_dir.iterdir()), (
            "a delivered page must stamp the cooldown even when earlier attempts failed"
        )

    def test_gives_up_loudly_after_max_attempts(self, tmp_path: Path):
        log = install_fake_curl(tmp_path, fail_first=99)
        env = base_env(tmp_path, ROBOTHOR_ALERT_MAX_ATTEMPTS="3", **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode != 0
        assert curl_calls(log) == 3
        assert "3" in result.stdout + result.stderr
        state_dir = tmp_path / "alert-cooldown"
        assert not state_dir.exists() or not any(state_dir.iterdir()), (
            "an undelivered page must not stamp the cooldown"
        )

    def test_each_attempt_uses_curl_transient_retry_flags(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        args = log.read_text()
        assert "--retry" in args
        assert "--retry-all-errors" in args

    def test_missing_token_is_retried_not_fatal_on_first_attempt(self, tmp_path: Path):
        """No token and no secrets file: keep trying for the whole budget —
        the secrets appear mid-boot, exactly when the pager is needed."""
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_ALERT_MAX_ATTEMPTS="3")
        result = run_send(tmp_path, env)
        assert result.returncode != 0
        assert "ROBOTHOR_TELEGRAM_BOT_TOKEN" in result.stdout + result.stderr
        assert curl_calls(log) == 0

    def test_secrets_are_resourced_inside_the_retry_loop(self, tmp_path: Path):
        """Start with no token and no secrets file; the file appears while the
        loop is sleeping (the boot decrypt finishing) — the page must go out
        with the recovered credentials."""
        log = install_fake_curl(tmp_path)
        secrets = tmp_path / "late-secrets.env"
        env = base_env(
            tmp_path,
            ROBOTHOR_ALERT_MAX_ATTEMPTS="20",
            ROBOTHOR_ALERT_RETRY_DELAY="1",
            ROBOTHOR_SECRETS_FILE=str(secrets),
        )

        def write_secrets_late() -> None:
            time.sleep(2.0)
            secrets.write_text(
                "ROBOTHOR_TELEGRAM_BOT_TOKEN=latetok\nROBOTHOR_TELEGRAM_CHAT_ID=42\n"
            )

        writer = threading.Thread(target=write_secrets_late)
        writer.start()
        try:
            result = subprocess.run(
                ["bash", str(SEND), "robothor-sample.service"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        finally:
            writer.join()
        assert result.returncode == 0, result.stdout + result.stderr
        assert "botlatetok/sendMessage" in log.read_text()

    def test_api_base_is_overridable_for_hermetic_tests(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_TELEGRAM_API_BASE="http://127.0.0.1:9999",
            **FAKE_TOKEN_ENV,
        )
        result = run_send(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "http://127.0.0.1:9999/bottok123/sendMessage" in log.read_text()


# ── cron-wrapper.sh: non-zero exits page instead of rotting in logs ──────────


def run_wrapper(
    tmp_path: Path, cmd: list[str], env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = base_env(tmp_path)
    # cron-wrapper sources instance/secrets env files — point both at
    # nonexistent tmp paths so a test never reads the box's real files.
    env["ROBOTHOR_INSTANCE_ENV"] = str(tmp_path / "no-such-robothor.env")
    env["USER"] = "testuser"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(WRAPPER), *cmd],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


class TestCronWrapperPagesOnFailure:
    def test_success_passes_through_and_does_not_page(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        result = run_wrapper(tmp_path, ["true"], FAKE_TOKEN_ENV)
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 0

    def test_failure_exit_code_is_preserved(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        result = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], FAKE_TOKEN_ENV)
        assert result.returncode == 7

    def test_failure_pages_with_command_name_and_exit_code(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        result = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], FAKE_TOKEN_ENV)
        assert result.returncode == 7
        assert curl_calls(log) == 1
        args = log.read_text()
        assert "sh" in args, "the page must name the failed command"
        assert "7" in args, "the page must carry the exit code"

    def test_a_broken_pager_does_not_change_the_exit_code(self, tmp_path: Path):
        """Fail-open: no token, no secrets — the alert fails internally but
        the cron job's own exit code must come through untouched."""
        install_fake_curl(tmp_path)
        result = run_wrapper(tmp_path, ["sh", "-c", "exit 7"])
        assert result.returncode == 7, result.stdout + result.stderr

    def test_stdout_of_the_wrapped_command_passes_through(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        result = run_wrapper(tmp_path, ["echo", "hello-from-cron"], FAKE_TOKEN_ENV)
        assert result.returncode == 0
        assert "hello-from-cron" in result.stdout


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory permissions, so 0555 is writable"
)
class TestCronDedupSurvivesAnUnwritableStateDir:
    """Cron failures paged with ZERO dedup.

    ``/run/robothor/alert-cooldown`` is root:root 0755 and cron runs as the
    operator's own user, so the ``touch`` at the end of the sender silently
    failed (``|| true``) and the stamp was never written. Every subsequent
    run re-read an empty state dir and paged again: a crontab entry pointing
    at a deleted script paged once a day for 129 days.

    The cooldown must therefore fall back to a dir the calling user CAN
    write — for both the read and the stamp, or the dedup is still half
    broken — and say which one it used, so the next person reading a cron
    log can find the stamps.
    """

    def unwritable_state(self, tmp_path: Path) -> dict[str, str]:
        ro = tmp_path / "root-owned-state"
        ro.mkdir()
        ro.chmod(0o555)
        env = dict(FAKE_TOKEN_ENV)
        env["ROBOTHOR_ALERT_STATE_DIR"] = str(ro)
        env["ROBOTHOR_ALERT_FALLBACK_STATE_DIR"] = str(tmp_path / "fallback-state")
        return env

    def test_a_repeated_cron_failure_pages_once(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = self.unwritable_state(tmp_path)

        first = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], env)
        assert first.returncode == 7, first.stdout + first.stderr
        assert curl_calls(log) == 1

        second = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], env)
        assert second.returncode == 7, second.stdout + second.stderr
        assert curl_calls(log) == 1, (
            "the same cron command failing twice paged twice — the cooldown "
            "stamp could not be written, so every run looks like the first"
        )
        assert "suppressed duplicate" in second.stdout + second.stderr

    def test_the_log_names_the_fallback_dir_it_used(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        env = self.unwritable_state(tmp_path)
        result = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], env)
        assert result.returncode == 7
        assert str(tmp_path / "fallback-state") in result.stdout + result.stderr, (
            "a cooldown that silently moves is a cooldown nobody can find"
        )

    def test_the_stamp_lands_in_the_fallback_dir(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        env = self.unwritable_state(tmp_path)
        run_wrapper(tmp_path, ["sh", "-c", "exit 7"], env)
        fallback = tmp_path / "fallback-state"
        assert fallback.exists() and list(fallback.iterdir()), (
            "nothing was stamped anywhere, so the next failure pages again"
        )
        mode = oct(fallback.stat().st_mode)[-3:]
        assert mode == "700", (
            f"fallback state dir must be created 0700 (no other user can read "
            f"or plant stamps in it), got {mode}"
        )

    def test_a_preexisting_symlinked_fallback_dir_is_not_adopted(self, tmp_path: Path):
        """A local user who pre-creates the fallback path as a symlink to
        another directory could plant stamps there to suppress a real page,
        or read stamps written through it. ``mkdir -m 700 -p`` succeeds
        silently on a symlink to a dir without touching its mode, so this
        must be rejected explicitly. The page must still be sent — dedup
        is disabled for this send instead."""
        log = install_fake_curl(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        fallback_path = tmp_path / "fallback-state"
        fallback_path.symlink_to(elsewhere)

        env = self.unwritable_state(tmp_path)
        env["ROBOTHOR_ALERT_FALLBACK_STATE_DIR"] = str(fallback_path)

        result = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], env)
        assert result.returncode == 7, result.stdout + result.stderr
        assert curl_calls(log) == 1, "a page must never be dropped over dedup state"
        assert (
            "fallback state dir" in result.stdout + result.stderr
            and "dedup disabled" in result.stdout + result.stderr
        ), "an untrusted fallback dir must be named and dedup explicitly disabled"
        assert list(elsewhere.iterdir()) == [], (
            "no stamp may be written through the symlink"
        )

    def test_a_preexisting_owned_directory_is_adopted_normally(self, tmp_path: Path):
        """A fallback dir that already exists, is a real directory (not a
        symlink), and is owned by this user must be trusted and used —
        not everything pre-existing is an attack."""
        log = install_fake_curl(tmp_path)
        fallback_path = tmp_path / "fallback-state"
        fallback_path.mkdir(mode=0o700)

        env = self.unwritable_state(tmp_path)
        env["ROBOTHOR_ALERT_FALLBACK_STATE_DIR"] = str(fallback_path)

        result = run_wrapper(tmp_path, ["sh", "-c", "exit 7"], env)
        assert result.returncode == 7, result.stdout + result.stderr
        assert curl_calls(log) == 1
        assert list(fallback_path.iterdir()), "the pre-existing owned dir was not adopted"
        assert "dedup disabled" not in result.stdout + result.stderr

    def test_a_writable_state_dir_is_used_unchanged(self, tmp_path: Path):
        """Root's path must not move: /run/robothor/alert-cooldown stays the
        stamp dir whenever it is writable."""
        install_fake_curl(tmp_path)
        env = dict(FAKE_TOKEN_ENV)
        result = run_send(tmp_path, base_env(tmp_path, **env))
        assert result.returncode == 0, result.stdout + result.stderr
        assert list((tmp_path / "alert-cooldown").iterdir()), "stamp left the writable dir"
        assert not (tmp_path / "alert-cooldown-fallback").exists(), (
            "a writable state dir must not trigger the fallback"
        )


# ── unit templates: the directives that survive the next boot ────────────────


def unit_text(name: str) -> str:
    path = UNIT_DIR / name
    assert path.exists(), f"infra/systemd/{name} missing"
    return path.read_text()


class TestAlertUnitRetriesItself:
    """systemd fires OnFailure exactly once; if the pager unit dies, the page
    is gone. The template must let systemd retry the pager itself."""

    def test_restarts_on_failure(self):
        text = unit_text("robothor-alert@.service")
        assert "Restart=on-failure" in text
        assert "RestartSec=60" in text

    def test_systemd_start_limit_is_disabled(self):
        """The bounded retry budget this test used to pin (5 starts/hour) was
        itself a silencer: on 2026-08-20 the alert unit hit 'start-limit-hit'
        60 times while two services crash-looped, so the crash loop muted its
        own pager for an hour. The sender dedups per unit on its own
        (ROBOTHOR_ALERT_COOLDOWN_SECONDS); the systemd limit only removed
        pages. See tests/test_liveness_watchdog.py."""
        lines = [
            line
            for line in unit_text("robothor-alert@.service").splitlines()
            if not line.lstrip().startswith(("#", ";"))
        ]
        assert "StartLimitIntervalSec=0" in lines
        assert not [line for line in lines if line.startswith("StartLimitBurst=")]

    def test_not_oneshot_because_oneshot_forbids_restart(self):
        # systemd rejects Restart= (other than no) on Type=oneshot units.
        text = unit_text("robothor-alert@.service")
        assert "Type=oneshot" not in text
        assert "Type=exec" in text


class TestBackupChainStopsRacingBootMounts:
    """Persistent=true timers fire at boot before /mnt/robothor-backup is
    mounted and before DNS is up — verify failed every catch-up run for ~3
    weeks and the failure page itself died on name resolution."""

    @pytest.mark.parametrize(
        "unit",
        [
            "robothor-backup-local.service",
            "robothor-backup-offsite.service",
            "robothor-backup-verify.service",
            "robothor-basebackup.service",
            "robothor-wal-offsite.service",
        ],
    )
    def test_mount_touching_units_require_the_mount(self, unit: str):
        assert "RequiresMountsFor=/mnt/robothor-backup" in unit_text(unit)

    @pytest.mark.parametrize(
        "unit",
        [
            "robothor-backup-offsite.service",
            "robothor-backup-verify.service",
            "robothor-wal-offsite.service",
        ],
    )
    def test_network_touching_units_wait_for_network_online(self, unit: str):
        text = unit_text(unit)
        assert "After=network-online.target" in text
        assert "Wants=network-online.target" in text


class TestAWedgedVolumeSkipsTheBackupUnits:
    """A mounted-but-wedged volume must SKIP the backup units, not fail them.

    When the encrypted USB volume drops off the bus, ext4 remounts it
    ``emergency_ro``: stat() keeps succeeding, so ``RequiresMountsFor=`` is
    satisfied and every in-script guard passes, but readdir() and write() fail.
    The units therefore ran and failed — robothor-wal-offsite every 15 minutes,
    96 OnFailure triggers a day, ~22 pages that said nothing but a unit name.

    ``ExecCondition=`` is the systemd primitive for "do not even try". Its exit
    codes are the whole design (systemd 255):

        exit 0        condition holds, unit runs
        exit 1-254    condition fails, unit is SKIPPED, OnFailure does NOT fire
        exit 255      the condition check itself failed, unit FAILS and pages

    so the probe's exit 1 is what turns the storm off, and its 255 keeps a
    genuinely broken probe loud. See tests/test_backup_volume_check.py.
    """

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_backup_units_gate_on_the_volume_probe(self, unit: str):
        conditions = [
            line
            for line in unit_text(unit).splitlines()
            if line.startswith("ExecCondition=")
        ]
        assert conditions, (
            f"{unit} has no ExecCondition= — a wedged volume makes it RUN and "
            "FAIL, and every failure pages the operator"
        )
        assert any("backup-volume-check.sh" in line for line in conditions), (
            f"{unit}'s ExecCondition= does not consult scripts/backup-volume-check.sh"
        )

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_the_probe_is_asked_for_the_access_the_unit_needs(self, unit: str):
        mode = VOLUME_GATED_UNITS[unit][0]
        line = volume_condition_line(unit)
        assert f" {mode} " in f"{line} ", (
            f"{unit} must probe with {mode}: a unit that writes to the volume is "
            "not protected by a read-only probe (emergency_ro passes every read)"
        )

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_the_condition_runs_before_the_work(self, unit: str):
        """systemd runs ExecCondition= before ExecStart= regardless of order,
        but a reader must not have to know that."""
        lines = unit_text(unit).splitlines()
        condition = next(
            i for i, line in enumerate(lines) if line.startswith("ExecCondition=")
        )
        start = next(
            i for i, line in enumerate(lines) if line.startswith("ExecStart=")
        )
        assert condition < start, f"{unit} declares ExecCondition= after ExecStart="

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_the_mount_dependency_is_kept(self, unit: str):
        """ExecCondition= replaces neither RequiresMountsFor= nor the boot
        ordering it fixed — it covers the state where the mount EXISTS and is
        useless, which RequiresMountsFor= cannot see."""
        assert "RequiresMountsFor=/mnt/robothor-backup" in unit_text(unit)

    def test_wal_offsite_is_not_gated_because_it_degrades_instead(self):
        """The WAL push is the 15-minute RPO and the WAL archive lives on NVMe,
        not on the backup volume. Skipping this unit when the USB volume is
        wedged would stop shipping WAL offsite — trading a paging storm for
        actual data loss. Instead wal-offsite.sh runs the same probe itself and
        degrades: it skips the basebackup replication and the WAL prune, still
        pushes WAL, and exits 0. See test_backup_pages_on_failure.py."""
        directives = [
            line
            for line in unit_text("robothor-wal-offsite.service").splitlines()
            if not line.lstrip().startswith(("#", ";"))
        ]
        assert not [line for line in directives if line.startswith("ExecCondition=")], (
            "gating robothor-wal-offsite on the backup volume would stop the "
            "15-minute WAL push whenever the USB volume is wedged"
        )


class TestTheGateCannotDeadlockOnAFreshVolume:
    """A unit must not gate on a path that only its own ExecStart= creates.

    ``robothor-backup-local`` gated on ``/mnt/robothor-backup/robothor`` —
    which ``scripts/backup-ssd.sh`` creates on line 54, AFTER the guard. On a
    fresh (or re-made) volume the directory does not exist, the probe answers
    "unhealthy", systemd records ``Result=exec-condition`` and SKIPS the unit,
    nothing is ever created, and the unit skips forever. Silently: a skipped
    unit does not fire ``OnFailure=``, which is the entire point of the gate.

    ``robothor-basebackup`` had the same shape one level deeper: it gated on
    ``.../robothor/basebackup``, created by ``pg-basebackup.sh`` itself.

    The gate therefore has to sit on something an EARLIER actor creates:

      * backup-local gates on the MOUNT ROOT — created by the mount. The
        probe's separate-mount check is what proves that root is the real
        volume and not an empty directory on the root filesystem.
      * basebackup gates on ``.../robothor`` — created nightly by
        backup-local, so the weekly base backup finds it.
      * offsite/verify keep ``--ro .../db``: it holds the dumps they replicate,
        and if it is absent there is nothing to replicate — skipping is right.
    """

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_the_unit_gates_on_the_agreed_path(self, unit: str):
        assert volume_gate_path(unit) == VOLUME_GATED_UNITS[unit][1], (
            f"{unit} gates on {volume_gate_path(unit)}"
        )

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_a_unit_never_gates_on_a_path_only_it_creates(self, unit: str):
        path = volume_gate_path(unit)
        assert path in PATH_CREATED_BY, (
            f"{unit} gates on {path}, whose creator nobody has written down — "
            "an ExecCondition= on a path nothing creates skips forever"
        )
        assert PATH_CREATED_BY[path] != UNIT_EXEC_SCRIPT[unit], (
            f"{unit} gates on {path}, which only its own ExecStart= "
            f"({UNIT_EXEC_SCRIPT[unit]}) creates. On a fresh volume the "
            "condition never holds, the unit is SKIPPED (not failed, so no "
            "page), and it never runs again"
        )

    @pytest.mark.parametrize("unit", sorted(VOLUME_GATED_UNITS))
    def test_the_unit_says_what_creates_the_path_it_gates_on(self, unit: str):
        """The next person to move this path needs the reason in front of
        them, not in a commit message."""
        text = unit_text(unit)
        comments = [line for line in text.splitlines() if line.lstrip().startswith("#")]
        assert any("created" in line.lower() for line in comments), (
            f"{unit} does not say what creates the path its ExecCondition= "
            "gates on — the bootstrap deadlock is invisible from the unit file"
        )


class TestShutdownAndBootPathTemplates:
    def test_app_treats_sigterm_exit_as_success(self):
        # Node exits 143 on SIGTERM; without SuccessExitStatus=143 every clean
        # stop is recorded as a unit failure, polluting failure telemetry.
        assert "SuccessExitStatus=143" in unit_text("robothor-app.service")

    def test_orchestrator_delivers_sigterm_to_the_main_process(self):
        # uvicorn never saw SIGTERM under KillMode=control-group and needed
        # SIGKILL after the 90s stop timeout on every shutdown.
        assert "KillMode=mixed" in unit_text("robothor-orchestrator.service")

    def test_xvfb_orders_after_the_gpu_driver(self):
        # Xvfb segfaulted on the first start of 3-of-3 recent boots (racing
        # NVIDIA driver init); the second start 5s later always succeeded.
        assert "nvidia-persistenced.service" in unit_text("robothor-xvfb.service")

    def test_vnc_disables_ipv6_listener(self):
        # x11vnc spammed ~200 rfbListenOnTCP6Port getaddrinfo errors per boot.
        assert "-noipv6" in unit_text("robothor-vnc.service")


# ── shell hygiene ────────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", [SEND, WRAPPER, VOLUME_CHECK, STATE_LIB, VOLUME_GUARD])
def test_changed_scripts_are_shellcheck_clean(script: Path):
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── send_failure_alert.sh: an HTTP error is not a delivered page ─────────────


def install_http_error_curl(tmp_path: Path, status: str = "401") -> Path:
    """Install a curl that TRANSFERS FINE but returns an HTTP error status.

    This is what a revoked bot token or a wrong chat_id actually looks like.
    curl exits 0 — it fetched the response body successfully; the body just
    happens to say ``{"ok":false,"error_code":401}``. Without ``--fail`` (or an
    explicit status check) the caller cannot tell this from a delivered page.

    The pre-existing fake curl only ever varied the EXIT CODE, which is why
    this whole class of failure had no coverage.
    """
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        "# Honour -w so a caller that asks for the status code gets it.\n"
        "want_code=0\n"
        'for a in "$@"; do [ "$a" = \'%{http_code}\' ] && want_code=1; done\n'
        f"[ \"$want_code\" = 1 ] && printf '{status}'\n"
        f'[ "$want_code" = 0 ] && printf \'{{"ok":false,"error_code":{status}}}\'\n'
        "exit 0\n"  # transport succeeded; the HTTP status is the failure
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


class TestHttpErrorIsNotDelivery:
    def test_http_401_is_not_treated_as_a_delivered_page(self, tmp_path: Path):
        """A revoked token must fail loudly, not report success."""
        install_http_error_curl(tmp_path, "401")
        env = base_env(tmp_path, **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode != 0, (
            "the pager exited 0 on HTTP 401 — systemd's Restart=on-failure will "
            "never retry, and this is the only paging path for 8 units"
        )

    def test_http_401_does_not_arm_the_cooldown_stamp(self, tmp_path: Path):
        """Arming the 1h cooldown on an undelivered page suppresses the next one."""
        install_http_error_curl(tmp_path, "401")
        state = tmp_path / "alert-cooldown"
        env = base_env(tmp_path, **FAKE_TOKEN_ENV)
        run_send(tmp_path, env)
        stamps = list(state.glob("*")) if state.exists() else []
        assert not stamps, f"cooldown armed on an undelivered page: {stamps}"

    def test_http_500_is_also_not_delivery(self, tmp_path: Path):
        install_http_error_curl(tmp_path, "500")
        env = base_env(tmp_path, **FAKE_TOKEN_ENV)
        assert run_send(tmp_path, env).returncode != 0

    def test_the_failure_names_the_http_status(self, tmp_path: Path):
        """'attempt failed' is not actionable; '401' tells the operator to rotate."""
        install_http_error_curl(tmp_path, "401")
        env = base_env(tmp_path, **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert "401" in (result.stderr + result.stdout), (
            "the HTTP status must appear in the log, or a dead token is "
            "indistinguishable from a network blip"
        )

    def test_a_real_2xx_still_succeeds_and_arms_the_cooldown(self, tmp_path: Path):
        """The happy path must be unchanged."""
        install_http_error_curl(tmp_path, "200")
        state = tmp_path / "alert-cooldown"
        env = base_env(tmp_path, **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode == 0
        assert state.exists() and list(state.glob("*")), "cooldown not armed on a real send"


# ── send_failure_alert.sh: DNS loss must not eat the page ────────────────────
# Since 2026-08-31 the journal carries 63 `curl_rc=6` lines — "Could not
# resolve host". `robothor-alert@.service` has Restart=on-failure behind it, so
# those pages come back. The callers WITHOUT a retrying unit behind them
# (scripts/cron-wrapper.sh, backup-offsite.sh, thermal-guard.sh, boot-guard.sh)
# have nothing to come back to: the retry loop exhausts, the script exits 1 and
# the page is gone.
#
# Longer backoff was rejected — it only helps the path that already retries. A
# pinned IP breaks on rotation, and curl's --dns-servers needs a c-ares build.
# So the undeliverable page goes to a durable spool on NVMe and the next
# successful send (or the 5-minute liveness tick) delivers it.


# Spool fixtures carry an epoch in the filename, and the drain now ages a page
# out after ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS (a day). A hard-coded epoch
# from 2026-08 was fine when nothing read it; today it would quarantine every
# fixture page unsent and the suite would be testing the age-out and nothing
# else. Recent, still ordered oldest-first.
SPOOLED_EPOCH_OLDER = int(time.time()) - 900
SPOOLED_EPOCH_NEWER = int(time.time()) - 300


def spool_dir(tmp_path: Path) -> Path:
    return tmp_path / "alert-spool"


def spooled(tmp_path: Path) -> list[Path]:
    """Spool files oldest-first, without assuming the filename scheme beyond
    the epoch prefix the drain order depends on."""
    d = spool_dir(tmp_path)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.name.endswith(".msg"))


def write_spooled(tmp_path: Path, epoch: int, text: str) -> Path:
    d = spool_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{epoch}-robothor-spooled.service.deadbeef.1.msg"
    path.write_text(text + "\n")
    return path


def run_drain(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SEND), "--drain"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


class TestUndeliverablePageIsSpooled:
    def test_base_env_pins_the_spool_dir(self, tmp_path: Path):
        """The spool is durable and drained later — an unpinned test would
        page the operator with fixture text on the next tick."""
        assert "ROBOTHOR_ALERT_SPOOL_DIR" in base_env(tmp_path)

    def test_an_exhausted_send_spools_the_page(self, tmp_path: Path):
        install_fake_curl(tmp_path, fail_first=99)
        result = run_send(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode != 0, "an undelivered page must still fail the caller"
        files = spooled(tmp_path)
        assert len(files) == 1, f"the page was not spooled: {result.stdout}{result.stderr}"
        assert "robothor-sample.service" in files[0].read_text()

    def test_the_spooled_text_is_the_page_the_operator_would_have_seen(self, tmp_path: Path):
        install_fake_curl(tmp_path, fail_first=99)
        run_send(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        text = spooled(tmp_path)[0].read_text()
        assert text.startswith("🔴 robothor-sample.service FAILED on ")
        assert "no consequence mapped" in text, "the consequence line must be spooled too"

    def test_a_missing_token_still_spools(self, tmp_path: Path):
        """The boot window that has no secrets is exactly the window whose
        pages were lost — the spool must catch that one too."""
        install_fake_curl(tmp_path)
        result = run_send(tmp_path, base_env(tmp_path))
        assert result.returncode != 0
        assert len(spooled(tmp_path)) == 1

    def test_a_delivered_page_spools_nothing(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        result = run_send(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert spooled(tmp_path) == []

    def test_a_suppressed_run_spools_nothing(self, tmp_path: Path):
        install_fake_curl(tmp_path, fail_first=99)
        env = base_env(tmp_path, ROBOTHOR_ALERT_SUPPRESS="1", **FAKE_TOKEN_ENV)
        result = run_send(tmp_path, env)
        assert result.returncode == 0
        assert spooled(tmp_path) == [], "a suppressed page must not come back via the spool"


class TestSpoolDrain:
    def test_drain_delivers_the_spooled_page_and_removes_it(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "🔴 robothor-spooled.service FAILED on box")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 1
        assert "robothor-spooled.service" in log.read_text()
        assert spooled(tmp_path) == [], "a delivered spool file must be deleted"

    def test_the_drained_page_says_it_is_delayed_and_when_it_was_queued(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        epoch = SPOOLED_EPOCH_OLDER
        write_spooled(tmp_path, epoch, "🔴 robothor-spooled.service FAILED on box")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        queued = time.strftime("%H:%M", time.localtime(epoch))
        assert f"⏳ DELAYED (queued {queued}):" in log.read_text()

    def test_an_undelivered_spool_file_is_kept(self, tmp_path: Path):
        install_fake_curl(tmp_path, fail_first=99)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "🔴 robothor-spooled.service FAILED on box")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert len(spooled(tmp_path)) == 1, "a failed drain must not delete the page"

    def test_drain_goes_oldest_first(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-NEWEST")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        args = log.read_text()
        assert args.index("PAGE-OLDEST") < args.index("PAGE-NEWEST")

    def test_drain_stops_at_the_first_failure(self, tmp_path: Path):
        """Telegram is still down: burning the whole spool against a dead
        endpoint would deliver nothing and lose the ordering."""
        log = install_fake_curl(tmp_path, fail_first=99)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-NEWEST")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert curl_calls(log) == 1, "the drain kept sending after a failure"
        assert len(spooled(tmp_path)) == 2

    def test_drain_ignores_the_cooldown(self, tmp_path: Path):
        """The cooldown dedups repeat pages for a live unit. A spooled page is
        one the operator has NEVER seen, so it must not be swallowed by a
        stamp armed while it sat on disk."""
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_ALERT_COOLDOWN_SECONDS="3600", **FAKE_TOKEN_ENV)
        assert run_send(tmp_path, env).returncode == 0  # arms the stamp
        assert curl_calls(log) == 1
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-SPOOLED")
        run_drain(tmp_path, env)
        assert "PAGE-SPOOLED" in log.read_text()

    def test_drain_is_silent_and_cheap_when_the_spool_is_empty(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 0

    def test_drain_without_credentials_keeps_the_spool(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-SPOOLED")
        result = run_drain(tmp_path, base_env(tmp_path))
        assert result.returncode == 0, "a drain that cannot run yet is not a failure"
        assert len(spooled(tmp_path)) == 1

    def test_drain_refuses_a_spooled_page_naming_a_pytest_path(self, tmp_path: Path):
        """The entry guard covers the unit name; the spool is a second way in.
        A fixture page that reached the disk must never reach the operator."""
        log = install_fake_curl(tmp_path)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "🔴 /tmp/pytest-of-someone/pytest-1/x FAILED")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 0, "a pytest fixture page was sent to the operator"
        assert spooled(tmp_path) == []


def install_status_curl(tmp_path: Path, statuses: list[str]) -> Path:
    """A curl stand-in that answers a SEQUENCE of HTTP statuses.

    The transport always succeeds (exit 0, like real curl on a 400) — only the
    status varies, which is the whole point: a 400 is the endpoint refusing
    THIS message, a 500 is the endpoint being unavailable, and the drain has
    to tell them apart.  The last status repeats once the list runs out.
    """
    log = tmp_path / "curl-args.txt"
    count = tmp_path / "curl-count.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    seq = " ".join(statuses)
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        f'n=$(cat "{count}" 2>/dev/null || echo 0)\n'
        f'n=$((n + 1)); echo "$n" > "{count}"\n'
        f"statuses=({seq})\n"
        'i=$((n - 1)); [ "$i" -ge "${#statuses[@]}" ] && i=$(( ${#statuses[@]} - 1 ))\n'
        'code="${statuses[$i]}"\n'
        # A separate log of what actually LANDED. The argv log records every
        # attempt, delivered or not, so asserting on it cannot tell a page the
        # operator received from one Telegram refused — the exact distinction
        # the drop notice turns on.
        f'case "$code" in 2*) printf \'%s\\n\' "$@" >> "{tmp_path / "curl-delivered.txt"}" ;; esac\n'
        "for a in \"$@\"; do [ \"$a\" = '%{http_code}' ] && printf '%s' \"$code\"; done\n"
        "exit 0\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def delivered_text(tmp_path: Path) -> str:
    """Everything the stub answered 2xx to — i.e. what the operator saw."""
    path = tmp_path / "curl-delivered.txt"
    return path.read_text() if path.exists() else ""


def poisoned(tmp_path: Path) -> list[Path]:
    d = spool_dir(tmp_path) / "poison"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file())


def attempts_of(path: Path) -> int:
    sidecar = Path(str(path) + ".attempts")
    return int(sidecar.read_text().strip()) if sidecar.exists() else 0


class TestARejectedPageDoesNotWedgeTheSpool:
    """A page Telegram REFUSES is not a page Telegram cannot take.

    The drain broke out of the loop on any post failure, with no per-file
    attempt counter and no age-out. So one permanently-rejected message — a
    body Telegram answers 400 to, e.g. one whose journal tail was cut through
    a multi-byte character — sat at the head of the queue forever and every
    page raised after it was never delivered. The spool's whole promise is
    that a page is late, not lost; this inverted it for the entire queue.

    Content rejection and unavailability need OPPOSITE handling: quarantine
    the message (nothing will ever make it acceptable) versus keep it and
    retry later (nothing is wrong with it).
    """

    def test_a_400_does_not_block_the_page_behind_it(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["400", "200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-REJECTED")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-BEHIND-IT")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        args = log.read_text()
        assert "PAGE-BEHIND-IT" in args, (
            "a page Telegram rejected stopped the drain, so every page queued "
            "behind it is undeliverable for as long as it sits there"
        )
        assert spooled(tmp_path) == [], "the delivered page was not removed"

    def test_a_rejected_page_is_quarantined_not_deleted(self, tmp_path: Path):
        install_status_curl(tmp_path, ["400", "200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-REJECTED")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-BEHIND-IT")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        poison = poisoned(tmp_path)
        assert len(poison) == 1, f"the rejected page was not quarantined: {poison}"
        assert "PAGE-REJECTED" in poison[0].read_text()

    def test_the_quarantine_is_loud(self, tmp_path: Path):
        install_status_curl(tmp_path, ["400", "200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-REJECTED")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        out = result.stdout + result.stderr
        assert "QUARANTINED" in out and "400" in out, (
            f"a page was dropped from the delivery path without saying so: {out}"
        )

    def test_a_quarantined_page_is_not_retried(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["400", "200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-REJECTED")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        before = curl_calls(log)
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert curl_calls(log) == before, "the poison directory is being drained"

    def test_a_500_still_stops_the_drain(self, tmp_path: Path):
        """Unavailability is the opposite case: the message is fine, the
        endpoint is not. Burning the queue against it delivers nothing and
        loses the ordering."""
        log = install_status_curl(tmp_path, ["500"])
        head = write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-NEWEST")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert curl_calls(log) == 1, "the drain kept sending after a 5xx"
        assert len(spooled(tmp_path)) == 2, "a 5xx must not discard a page"
        assert poisoned(tmp_path) == [], "a 5xx is not a content rejection"
        assert attempts_of(head) == 1, "the failure was not counted against the file"

    def test_a_401_does_not_poison_the_whole_spool(self, tmp_path: Path):
        """A revoked token answers 4xx to EVERY page. Quarantining on that
        would empty the spool into poison/ on one bad credential — the
        opposite of what the spool is for."""
        log = install_status_curl(tmp_path, ["401"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-NEWEST")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert curl_calls(log) == 1
        assert len(spooled(tmp_path)) == 2
        assert poisoned(tmp_path) == []

    def test_attempts_accumulate_across_drains(self, tmp_path: Path):
        install_status_curl(tmp_path, ["500"])
        head = write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        for _ in range(3):
            run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert attempts_of(head) == 3

    def test_a_file_past_the_attempt_budget_is_quarantined(self, tmp_path: Path):
        """~48 ticks is about 4h of 5-minute drains. A page nothing has been
        able to deliver in 4h is not going out; it must stop holding the
        queue."""
        log = install_status_curl(tmp_path, ["500", "500", "200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-STUCK")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-BEHIND-IT")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS="2", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)  # attempt 1
        run_drain(tmp_path, env)  # attempt 2 — budget spent
        result = run_drain(tmp_path, env)  # quarantine, then deliver the next
        out = result.stdout + result.stderr
        assert "QUARANTINED" in out, out
        assert len(poisoned(tmp_path)) == 1
        assert "PAGE-BEHIND-IT" in log.read_text()
        assert spooled(tmp_path) == []

    def test_the_attempt_sidecar_goes_with_the_quarantined_file(self, tmp_path: Path):
        install_status_curl(tmp_path, ["500", "200"])
        head = write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-STUCK")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS="1", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)
        run_drain(tmp_path, env)
        assert not Path(str(head) + ".attempts").exists(), (
            "the sidecar outlived its page — the spool dir accumulates litter "
            "no drain will ever clean"
        )

    def test_a_page_older_than_the_age_cap_is_quarantined_unsent(self, tmp_path: Path):
        """A day-old page is not an incident report any more; delivering it
        after the fact is noise, and holding the queue for it is worse."""
        log = install_status_curl(tmp_path, ["200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-ANCIENT")
        env = base_env(
            tmp_path, ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS="60", **FAKE_TOKEN_ENV
        )
        result = run_drain(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 0, "an aged-out page was still sent"
        assert len(poisoned(tmp_path)) == 1
        assert "QUARANTINED" in result.stdout + result.stderr

    def test_the_default_age_cap_is_a_day(self, tmp_path: Path):
        """Every other test pins the cap, so the shipped default needs its own
        probe — an unbounded default would be the wedge again, slower."""
        log = install_status_curl(tmp_path, ["200"])
        write_spooled(tmp_path, int(time.time()) - 2 * 86400, "PAGE-TWO-DAYS-OLD")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 0
        assert len(poisoned(tmp_path)) == 1

    def test_the_default_attempt_budget_is_forty_eight(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["200"])
        head = write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-STUCK")
        Path(str(head) + ".attempts").write_text("48\n")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 0, "a page at the attempt budget was sent anyway"
        assert len(poisoned(tmp_path)) == 1

    def test_a_page_short_of_the_budget_is_still_tried(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["200"])
        head = write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-NEARLY-STUCK")
        Path(str(head) + ".attempts").write_text("47\n")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert curl_calls(log) == 1, "the budget is off by one — a page was given up early"
        assert poisoned(tmp_path) == []

    def test_a_fresh_page_is_not_aged_out(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["200"])
        write_spooled(tmp_path, int(time.time()), "PAGE-FRESH")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_calls(log) == 1
        assert poisoned(tmp_path) == []

    def test_the_still_spooled_count_excludes_what_it_quarantined(self, tmp_path: Path):
        """"3 page(s) still spooled" when two are.

        The count was the size of the queue this drain STARTED with, minus
        what it delivered — so every page it quarantined on the way through
        was still being reported as waiting. That number is the one line an
        operator reads to decide whether the backlog is growing, and it
        overstated it by exactly the pages that had just been given up on.
        """
        install_status_curl(tmp_path, ["500"])
        write_spooled(tmp_path, int(time.time()) - 2 * 86400, "PAGE-ANCIENT")
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-NEWEST")

        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        out = result.stdout + result.stderr

        assert len(poisoned(tmp_path)) == 1, out
        assert len(spooled(tmp_path)) == 2, out
        assert "2 page(s) still spooled" in out, (
            "the count includes the page this drain quarantined — it is no "
            f"longer spooled, and nothing will retry it:\n{out}"
        )


def test_the_drain_skips_spool_files_the_current_user_does_not_own():
    """Only root drains another account's pages.

    The spool is 1777 sticky: root's units and the operator's cron jobs both
    write into it, and sticky means neither can unlink the other's files. A
    non-root drain that picks up a root-owned page therefore delivers it,
    fails to remove it, and delivers it again on the next tick — forever.
    Root's own 5-minute drain is what clears those.

    Asserted against the source: a file owned by another uid cannot be created
    from an unprivileged test, and this must not become a test that only runs
    under sudo. The behaviour it guards — the delivered-file unlink check —
    is exercised for real in
    TestTheDrainNeverCountsAPageItCannotRemove.
    """
    src = SEND.read_text()
    assert '-O "$f"' in src, (
        "the drain does not check spool-file ownership, so the operator's "
        "drain will re-send root's pages on every tick"
    )
    assert "EUID" in src, "the ownership filter must not apply to root, which drains everything"


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root can unlink anything; this is the operator's case"
)
class TestTheDrainNeverCountsAPageItCannotRemove:
    """`rm -f "$f" ... || true`, then count a delivery.

    In the 1777 sticky spool the operator's account cannot delete a
    root-spooled file. The drain deleted-and-shrugged, counted the page as
    delivered, and logged success — so the same page went out again on every
    operator-run drain, and the log said everything was fine.
    """

    def test_a_delivered_page_that_cannot_be_removed_stops_the_drain(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-UNDELETABLE")
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-BEHIND-IT")
        spool_dir(tmp_path).chmod(0o500)  # readable, listable, NOT writable
        try:
            result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        finally:
            spool_dir(tmp_path).chmod(0o700)
        out = result.stdout + result.stderr
        assert "cannot remove delivered spool file" in out, (
            f"the failed unlink was swallowed; the page will be re-sent forever: {out}"
        )
        assert curl_calls(log) == 1, (
            "the drain carried on past a page it could not remove — every page "
            "it 'delivers' from here is one it will deliver again next tick"
        )

    def test_it_does_not_report_pages_it_will_send_again_as_drained(self, tmp_path: Path):
        install_status_curl(tmp_path, ["200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-UNDELETABLE")
        spool_dir(tmp_path).chmod(0o500)
        try:
            result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        finally:
            spool_dir(tmp_path).chmod(0o700)
        out = result.stdout + result.stderr
        assert "drained 1 spooled page" not in out, (
            "a page still on disk was counted as drained — the log reads as a "
            "clean delivery of a page the operator is about to receive again"
        )

    def test_an_over_cap_page_that_cannot_be_removed_is_named_not_counted(
        self, tmp_path: Path
    ):
        """Same unchecked `rm` on the cap-drop path: it announced N pages
        dropped while dropping none of them."""
        install_status_curl(tmp_path, ["200"])
        for i in range(5):
            write_spooled(tmp_path, SPOOLED_EPOCH_OLDER + i, f"PAGE-{i}")
        spool_dir(tmp_path).chmod(0o500)
        try:
            result = run_drain(
                tmp_path, base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_CAP="2", **FAKE_TOKEN_ENV)
            )
        finally:
            spool_dir(tmp_path).chmod(0o700)
        out = result.stdout + result.stderr
        assert "cannot remove over-cap spool file" in out, out
        assert "3 older pages dropped" not in out, (
            "the pager announced a truncation it did not perform"
        )
        assert len(spooled(tmp_path)) == 5, "pages vanished from a read-only spool dir"


class TestSpoolCap:
    def test_the_spool_is_capped_and_says_what_it_dropped(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        for i in range(5):
            write_spooled(tmp_path, SPOOLED_EPOCH_OLDER + i, f"PAGE-{i}")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_CAP="2", **FAKE_TOKEN_ENV)
        result = run_drain(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout + result.stderr
        assert "3 older pages dropped" in out, out
        args = log.read_text()
        assert "3 older pages dropped" in args, "the drop must be paged, not only logged"
        assert "PAGE-0" not in args and "PAGE-2" not in args
        assert "PAGE-3" in args and "PAGE-4" in args
        assert spooled(tmp_path) == []

    def test_the_default_cap_is_fifty(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        for i in range(51):
            write_spooled(tmp_path, SPOOLED_EPOCH_OLDER + i, f"PAGE-{i}")
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert "1 older pages dropped" in result.stdout + result.stderr


class TestAnUnreadableSpoolFileIsNamedNotDeleted:
    """A `.msg` the drain cannot read used to be `rm`'d in silence.

    That is a page — the operator was owed it, nobody ever saw it, and the
    only record of its existence was deleted along with it. Whatever went
    wrong (a truncated write, a permission the spooler did not have) is
    diagnosable only from the file that is left behind.
    """

    def unreadable_page(self, tmp_path: Path) -> Path:
        d = spool_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{SPOOLED_EPOCH_OLDER}-robothor-truncated.service.deadbeef.1.msg"
        path.write_text("")
        return path

    def test_the_file_survives_the_drain(self, tmp_path: Path):
        install_status_curl(tmp_path, ["200"])
        path = self.unreadable_page(tmp_path)
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert path.exists(), "an unreadable page was deleted, taking the evidence with it"

    def test_the_drain_names_it(self, tmp_path: Path):
        install_status_curl(tmp_path, ["200"])
        path = self.unreadable_page(tmp_path)
        result = run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        out = result.stdout + result.stderr
        assert path.name in out, f"the skipped page was not named: {out}"

    def test_it_does_not_block_the_page_behind_it(self, tmp_path: Path):
        log = install_status_curl(tmp_path, ["200"])
        self.unreadable_page(tmp_path)
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-BEHIND-IT")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert "PAGE-BEHIND-IT" in delivered_text(tmp_path)
        assert curl_calls(log) == 1

    def test_it_leaves_the_queue_when_it_ages_out(self, tmp_path: Path):
        """Kept is not kept forever — the age cap is what finally clears it,
        into poison/ where it is still readable."""
        install_status_curl(tmp_path, ["200"])
        self.unreadable_page(tmp_path)
        env = base_env(
            tmp_path, ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS="60", **FAKE_TOKEN_ENV
        )
        run_drain(tmp_path, env)
        assert len(poisoned(tmp_path)) == 1


class TestTheSpoolDirIsCreatedUsableByBothAccounts:
    """`mkdir -p` alone leaves the spool 0755 and owned by whoever spooled
    first. Root's units and the operator's cron jobs both write here, so the
    other account is then silently unable to spool at all — and the pages that
    DID reach disk would be exactly the ones nobody was missing. The tmpfiles
    row normally creates it 1777; this is the path where it does not exist
    yet."""

    def test_a_spool_dir_this_run_creates_is_sticky_and_world_writable(
        self, tmp_path: Path
    ):
        install_fake_curl(tmp_path, fail_first=99)
        result = run_send(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode != 0
        assert spooled(tmp_path), "nothing was spooled — this test proves nothing"
        mode = spool_dir(tmp_path).stat().st_mode & 0o7777
        assert mode == 0o1777, f"a freshly created spool is {oct(mode)}, not 0o1777"

    def test_the_poison_dir_is_created_the_same_way(self, tmp_path: Path):
        """Root's tick and the operator's drain both quarantine into it; a
        0755 dir made by whichever ran first leaves the other unable to move
        anything aside, so its rejected pages stay in the queue."""
        install_status_curl(tmp_path, ["400"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-REJECTED")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert poisoned(tmp_path), "nothing was quarantined — this test proves nothing"
        mode = (spool_dir(tmp_path) / "poison").stat().st_mode & 0o7777
        assert mode == 0o1777, f"the poison dir is {oct(mode)}, not 0o1777"

    def test_an_existing_spool_dir_keeps_the_mode_its_owner_chose(self, tmp_path: Path):
        """Only the dir we just created is ours to chmod. Widening a directory
        somebody else made — to 1777, of all modes — is not the pager's call."""
        d = spool_dir(tmp_path)
        d.mkdir(parents=True)
        d.chmod(0o700)
        install_fake_curl(tmp_path, fail_first=99)
        run_send(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert spooled(tmp_path), "nothing was spooled — this test proves nothing"
        mode = d.stat().st_mode & 0o7777
        assert mode == 0o700, f"the pager chmod'd an existing directory to {oct(mode)}"


class TestTheDropNoticeSurvivesTheOutage:
    """The truncation notice was posted at the moment of the drop.

    A drop happens because the spool overflowed, and the spool overflows
    because nothing has been deliverable for a long time — so the one moment
    the notice is raised is the one moment it cannot be sent. It went to
    stderr of a cron job nobody reads, and the operator was never told that
    N pages had been thrown away. A silent truncation is the pager lying
    about what it held.
    """

    def drop_during_an_outage(self, tmp_path: Path) -> None:
        install_status_curl(tmp_path, ["500"])
        for i in range(5):
            write_spooled(tmp_path, SPOOLED_EPOCH_OLDER + i, f"PAGE-{i}")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_CAP="2", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)
        assert "3 older pages dropped" not in delivered_text(tmp_path), (
            "this test is not exercising an outage — the notice went out"
        )

    def test_the_notice_arrives_when_the_endpoint_comes_back(self, tmp_path: Path):
        self.drop_during_an_outage(tmp_path)
        install_status_curl(tmp_path, ["200"])  # the endpoint recovers
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_CAP="2", **FAKE_TOKEN_ENV)
        result = run_drain(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 older pages dropped" in delivered_text(tmp_path), (
            "the operator was never told that 3 pages were thrown away"
        )

    def test_the_notice_arrives_exactly_once(self, tmp_path: Path):
        self.drop_during_an_outage(tmp_path)
        install_status_curl(tmp_path, ["200"])
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_CAP="2", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)
        write_spooled(tmp_path, SPOOLED_EPOCH_NEWER, "PAGE-LATER")
        run_drain(tmp_path, env)
        assert delivered_text(tmp_path).count("3 older pages dropped") == 1, (
            "the pending-drop counter was not cleared; every later page now "
            "carries a notice about a truncation the operator already heard about"
        )

    def test_the_pending_count_is_kept_where_the_operator_can_see_it(self, tmp_path: Path):
        self.drop_during_an_outage(tmp_path)
        note = spool_dir(tmp_path) / ".dropped"
        assert note.exists(), "the drop was recorded nowhere the next drain can find it"
        assert "3" in note.read_text()

    def test_drops_across_several_outage_ticks_add_up(self, tmp_path: Path):
        self.drop_during_an_outage(tmp_path)  # 3 dropped, 2 left
        for i in range(3):
            write_spooled(tmp_path, SPOOLED_EPOCH_NEWER + i, f"PAGE-LATE-{i}")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_CAP="2", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)  # still 500: 3 more over the cap
        install_status_curl(tmp_path, ["200"])
        run_drain(tmp_path, env)
        assert "6 older pages dropped" in delivered_text(tmp_path), (
            "a second drop during the same outage overwrote the first count"
        )


class TestEveryNormalSendDrainsFirst:
    def test_a_normal_send_drains_the_spool_before_paging(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-SPOOLED")
        result = run_send(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        args = log.read_text()
        assert "PAGE-SPOOLED" in args, "the send did not drain the spool"
        assert args.index("PAGE-SPOOLED") < args.index("robothor-sample.service FAILED")
        assert spooled(tmp_path) == []

    def test_a_cooldown_suppressed_send_still_drains(self, tmp_path: Path):
        """The drain must sit ABOVE the cooldown check: a flapping unit inside
        its 1h cooldown is exactly when the spool would otherwise sit."""
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_ALERT_COOLDOWN_SECONDS="3600", **FAKE_TOKEN_ENV)
        assert run_send(tmp_path, env).returncode == 0
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-SPOOLED")
        result = run_send(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "suppressed duplicate page" in result.stdout + result.stderr
        assert "PAGE-SPOOLED" in log.read_text()
        assert spooled(tmp_path) == []


# ── the spool directory has to exist before the first outage ─────────────────


TMPFILES_DIR = REPO_ROOT / "infra" / "tmpfiles"


def tmpfiles_rows() -> list[list[str]]:
    """Every shipped tmpfiles.d row: TYPE PATH MODE USER GROUP AGE ARG."""
    rows: list[list[str]] = []
    for conf in sorted(TMPFILES_DIR.glob("*.conf")):
        for line in conf.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            if len(fields) >= 5 and fields[1].startswith("/"):
                rows.append(fields)
    return rows


def test_the_spool_dir_is_created_sticky_by_tmpfiles():
    """Root's units and the operator's cron jobs both spool here, and neither
    can chown the other's files. 1777 lets both write; the sticky bit keeps
    each able to delete only its own — and the drain deletes only what it
    delivered.

    Without a shipped tmpfiles row the directory is created ad hoc by whoever
    spools first, owned by that account, and the other side silently cannot
    write: the pages that DID reach disk would be exactly the ones nobody was
    missing.
    """
    default = "/var/lib/robothor/alert-spool"
    assert f"ROBOTHOR_ALERT_SPOOL_DIR:-{default}" in SEND.read_text(), (
        "the sender's default spool path moved — update the tmpfiles row with it"
    )
    rows = [r for r in tmpfiles_rows() if r[1] == default]
    assert rows, f"no tmpfiles.d row creates {default}"
    fields = rows[0]
    assert fields[0] == "d", f"{default} must be a directory row, not {fields[0]!r}"
    assert fields[2] == "1777", (
        f"the spool is {fields[2]}, not 1777 — a non-sticky world-writable dir "
        "lets any local user delete another's undelivered page, and a "
        "non-world-writable one drops every page raised by the other account"
    )


# ── the entry guard has to read the BODY too ─────────────────────────────────


def test_the_entry_guard_refuses_a_pytest_path_in_the_body(tmp_path: Path):
    """The two-argument form makes the BODY the page — and the guard only
    ever looked at the unit name.

    So the caller that passes a clean dedup key and a body composed from a
    path — the backup volume guard, reporting the volume it found unmounted —
    walks straight past a guard written for the one-argument shape. Under
    pytest that body is a tmpdir, which is exactly the text the guard exists
    to keep off the operator's phone.
    """
    log = install_fake_curl(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(SEND),
            "robothor-backup-volume",
            "🔴 backup volume /tmp/pytest-of-someone/pytest-7/vol0 is not mounted",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=base_env(tmp_path, **FAKE_TOKEN_ENV),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert curl_calls(log) == 0, (
        "a fixture path in the BODY was paged to the operator — the guard "
        "only inspected the dedup key"
    )
    assert spooled(tmp_path) == [], (
        "refused, then spooled: the next liveness drain would deliver it "
        "anyway, five minutes later"
    )


def test_a_real_body_still_pages(tmp_path: Path):
    """The guard stays narrow: it must refuse fixture paths, not bodies."""
    log = install_fake_curl(tmp_path)
    result = subprocess.run(
        ["bash", str(SEND), "robothor-backup-volume", "🔴 backup volume is not mounted"],
        capture_output=True,
        text=True,
        timeout=60,
        env=base_env(tmp_path, **FAKE_TOKEN_ENV),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert curl_calls(log) == 1, "a genuine two-argument page was suppressed"


# ── a stuck spool has to have an operator-visible surface ────────────────────


def stuck_note(tmp_path: Path) -> Path:
    return spool_dir(tmp_path) / ".stuck"


class TestAStuckSpoolMarksItself:
    """`--drain` exits 0 whatever happens, and drain_spool() returns 0 by
    design — a drain that cannot run is a backlog, not an incident, and
    failing the liveness unit here would fire an OnFailure page about the very
    outage that filled the spool.

    The cost is that a dead credential or a day-old queue produces nothing but
    journal lines on a box nobody is reading. The spool's promise is that a
    page is LATE, not lost; a queue nothing can move breaks that promise
    silently, which is the failure mode this whole file exists to refuse.

    So the drain leaves a marker when it gives up mid-queue, and the liveness
    probe (tests/test_liveness_watchdog.py) turns a marker that has survived
    30 minutes into a page of its own.
    """

    def test_one_failed_drain_is_not_stuck(self, tmp_path: Path):
        """A blip is not a stuck queue — the same discipline as the probe's
        consecutive-failure counting."""
        install_status_curl(tmp_path, ["401"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS="4", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)
        assert not stuck_note(tmp_path).exists(), (
            "one failed drain raised the alarm — an alarm that fires on every "
            "DNS blip is one the operator learns to ignore"
        )

    def test_half_the_attempt_budget_marks_the_spool_stuck(self, tmp_path: Path):
        """Two 401s against a 4-attempt budget: the token is wrong, and no
        amount of retrying will fix it."""
        install_status_curl(tmp_path, ["401"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        env = base_env(tmp_path, ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS="4", **FAKE_TOKEN_ENV)
        run_drain(tmp_path, env)
        run_drain(tmp_path, env)

        note = stuck_note(tmp_path)
        assert note.exists(), (
            "half the retry budget spent on the head of the queue and nothing "
            "says so anywhere an operator will look"
        )
        text = note.read_text()
        assert text.strip().count("\n") == 0, f"the marker must be one line: {text!r}"
        assert "401" in text, f"the marker must name why the queue is not moving: {text!r}"
        assert re.search(r"\d{4}-\d{2}-\d{2}T", text), (
            f"the marker must say when it was raised: {text!r}"
        )

    def test_an_old_head_page_marks_the_spool_stuck_before_the_budget(self, tmp_path: Path):
        """The budget is ~4h of ticks; the age cap is a day. A queue whose head
        has been waiting half a day is stuck whatever the attempt count says —
        a drain that only runs occasionally never spends its budget."""
        install_status_curl(tmp_path, ["500"])
        write_spooled(tmp_path, int(time.time()) - 50_000, "PAGE-OLD")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert stuck_note(tmp_path).exists(), (
            "the head page has been queued past half the age cap and the spool "
            "still reports itself healthy"
        )

    def test_a_delivered_page_clears_the_marker(self, tmp_path: Path):
        """Cleared by a DELIVERY, not by an attempt: the marker exists to
        outlive the outage's log lines, and a probe that pages about a queue
        which is moving again is the fatigue this pager keeps relearning."""
        install_status_curl(tmp_path, ["200"])
        write_spooled(tmp_path, SPOOLED_EPOCH_OLDER, "PAGE-OLDEST")
        stuck_note(tmp_path).write_text("2026-09-02T14:40:00+00:00 head page stuck\n")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert not stuck_note(tmp_path).exists(), (
            "the spool drained and the stuck marker survived it — the liveness "
            "probe will page about a queue that is empty"
        )

    def test_an_empty_spool_clears_the_marker(self, tmp_path: Path):
        """The last page can also leave the queue by being quarantined, and a
        marker with nothing behind it pages about nothing."""
        install_status_curl(tmp_path, ["200"])
        spool_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        stuck_note(tmp_path).write_text("2026-09-02T14:40:00+00:00 head page stuck\n")
        run_drain(tmp_path, base_env(tmp_path, **FAKE_TOKEN_ENV))
        assert not stuck_note(tmp_path).exists()
