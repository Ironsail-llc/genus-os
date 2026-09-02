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
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

# The backup units whose ExecCondition= must consult the volume probe, and the
# access mode each of them actually needs. wal-offsite is deliberately absent —
# see TestAWedgedVolumeSkipsTheBackupUnits.
VOLUME_GATED_UNITS = {
    "robothor-backup-local.service": "--rw",
    "robothor-basebackup.service": "--rw",
    "robothor-backup-offsite.service": "--ro",
    "robothor-backup-verify.service": "--ro",
}

FAKE_TOKEN_ENV = {
    "ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123",
    "ROBOTHOR_TELEGRAM_CHAT_ID": "42",
}


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
        "HOME": str(tmp_path),
        # Never touch the real /run/robothor state or secrets from a test.
        "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
        "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
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
        mode = VOLUME_GATED_UNITS[unit]
        line = next(
            line
            for line in unit_text(unit).splitlines()
            if line.startswith("ExecCondition=") and "backup-volume-check.sh" in line
        )
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
@pytest.mark.parametrize("script", [SEND, WRAPPER, VOLUME_CHECK])
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
