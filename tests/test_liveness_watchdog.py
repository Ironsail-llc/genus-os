"""The pager has to survive the exact failures it exists to report.

Two live failure modes, both observed in this box's journal:

1. START-LIMIT SELF-SILENCING. ``robothor-alert@.service`` carried
   ``StartLimitIntervalSec=3600`` / ``StartLimitBurst=5``. On 2026-08-20 the
   alert unit itself hit that limit 60 times::

       robothor-alert@robothor-orchestrator.service.service: Failed with
           result 'start-limit-hit'.   (31x)
       robothor-alert@robothor-bridge.service.service: Failed with
           result 'start-limit-hit'.   (29x)

   A crash-looping service therefore SILENCES ITS OWN PAGER FOR AN HOUR —
   precisely the case the pager exists for. The sender already dedups per
   unit for an hour (``ROBOTHOR_ALERT_COOLDOWN_SECONDS``), so the systemd
   start limit adds no protection at all, only silence.

2. ONFAILURE NEVER FIRES ON A HARD KILL. On 2026-08-19 13:50, during a
   shutdown/boot transaction, systemd logged::

       robothor-engine.service: Failed to enqueue OnFailure= job, ignoring:
           Transaction for robothor-alert@robothor-engine.service.service/start
           is destructive (...)

   No page was sent. OnFailure= is a single, best-effort, in-band hook: a
   SIGKILL, a destructive transaction, or a wedged-but-running process all
   produce silence.

So there is a second, independent path: ``scripts/liveness_probe.sh``, run by
``robothor-liveness.timer`` every few minutes, which probes the engine's
unauthenticated ``/live`` endpoint and pages through the SAME sender after N
consecutive failures. It depends on nothing inside the engine and nothing
inside systemd's failure plumbing.

The counting discipline is the whole point: a single blip must not page (or
the pager gets muted by fatigue), N consecutive failures must page, and a
recovery must reset the count. And, mirroring
``robothor/engine/alerts.py`` (``delivered = bool(sent)``), an undelivered
page is NOT success — the probe checks the sender's exit status instead of
assuming it worked.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "scripts" / "liveness_probe.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

# A unit name that does not exist on any box, so the sender's `journalctl -u`
# returns nothing and no host journal content is pulled into a test.
FAKE_UNIT = "robothor-nonexistent-probe-target.service"


# ── fakes ────────────────────────────────────────────────────────────────────


def install_fake_curl(tmp_path: Path) -> Path:
    """Install a curl stand-in on PATH serving BOTH roles in this pipeline.

    The probe curls the health endpoint and the sender curls the Telegram API;
    one stub records every argv and picks its exit code from the environment
    (``FAKE_CURL_PROBE_RC`` / ``FAKE_CURL_SEND_RC``), so a test can make the
    engine look dead while the pager still works, or vice versa.
    """
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        # Real curl with -w '%{http_code}' always prints a status, and the pager
        # now checks it (an HTTP 401 exits 0 but is NOT a delivered page). A
        # silent double makes the status read as 0 and every send look failed.
        # Map the simulated exit code onto a plausible status so both the exit
        # code and the status agree, the way they do in production.
        'send_rc="${FAKE_CURL_SEND_RC:-0}"\n'
        'probe_rc="${FAKE_CURL_PROBE_RC:-0}"\n'
        "want_code=0\n"
        'for a in "$@"; do [ "$a" = \'%{http_code}\' ] && want_code=1; done\n'
        'for a in "$@"; do\n'
        '    case "$a" in\n'
        "        *sendMessage*)\n"
        '            if [ "$want_code" = 1 ]; then\n'
        "                [ \"$send_rc\" = 0 ] && printf '200' || printf '000'\n"
        "            fi\n"
        '            exit "$send_rc" ;;\n'
        "    esac\n"
        "done\n"
        'if [ "$want_code" = 1 ]; then\n'
        "    [ \"$probe_rc\" = 0 ] && printf '200' || printf '000'\n"
        "fi\n"
        'exit "$probe_rc"\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def install_recording_alert(tmp_path: Path, exit_code: int = 0) -> Path:
    """A stand-in pager that records its argv — for asserting WHAT gets paged
    without dragging the real sender (and journalctl) into the test."""
    log = tmp_path / "alert-args.txt"
    alert = tmp_path / "bin" / "fake-alert.sh"
    alert.parent.mkdir(parents=True, exist_ok=True)
    alert.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\nexit {exit_code}\n')
    alert.chmod(alert.stat().st_mode | stat.S_IEXEC)
    return log


def send_attempts(log: Path) -> int:
    """Telegram sends the pipeline actually attempted — one `.../sendMessage`
    argument per curl invocation, logged whether or not the send succeeds.
    With a working stub (the default) attempts == pages delivered."""
    if not log.exists():
        return 0
    return log.read_text().count("/sendMessage")


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Hermetic environment: nothing here may touch the box's real /run state,
    real secrets, or the real Telegram API."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            # Probe state (the consecutive-failure counter).
            "ROBOTHOR_LIVENESS_STATE_DIR": str(tmp_path / "liveness"),
            "ROBOTHOR_LIVENESS_FAILURE_THRESHOLD": "3",
            "ROBOTHOR_LIVENESS_UNIT": FAKE_UNIT,
            # Sender isolation — the real cooldown dir is /run/robothor, and a
            # stamp written there by a test could suppress a REAL page later.
            "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
            # The probe drains the sender's spool on every tick, and the
            # sender spools any page it could not deliver. Both ends of that
            # loop have to point at this test's tmpdir: the real spool is
            # durable (/var/lib) and a page left there by a test WILL be
            # delivered to the operator by the next real tick.
            "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
            "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
            "ROBOTHOR_ALERT_MAX_ATTEMPTS": "1",
            "ROBOTHOR_ALERT_RETRY_DELAY": "0",
            "ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123",
            "ROBOTHOR_TELEGRAM_CHAT_ID": "42",
        }
    )
    env.update(extra)
    return env


def run_probe(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PROBE)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def failure_count(tmp_path: Path) -> int:
    """Read the consecutive-failure counter without assuming its filename —
    the on-disk naming scheme is the script's business, the count is not."""
    state_dir = tmp_path / "liveness"
    if not state_dir.exists():
        return 0
    files = [p for p in state_dir.iterdir() if p.is_file()]
    assert len(files) <= 1, f"expected one counter file, found {files}"
    if not files:
        return 0
    return int(files[0].read_text().strip() or 0)


DEAD = {"FAKE_CURL_PROBE_RC": "1"}
ALIVE = {"FAKE_CURL_PROBE_RC": "0"}


# ── the counting discipline ──────────────────────────────────────────────────


class TestConsecutiveFailureThreshold:
    def test_probe_script_exists_and_is_executable(self):
        assert PROBE.exists(), "scripts/liveness_probe.sh missing"
        assert PROBE.stat().st_mode & 0o111, f"{PROBE} is not executable"

    def test_a_healthy_engine_pages_nobody(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        result = run_probe(base_env(tmp_path, **ALIVE))
        assert result.returncode == 0, result.stdout + result.stderr
        assert send_attempts(log) == 0
        assert failure_count(tmp_path) == 0

    def test_a_single_blip_does_not_page(self, tmp_path: Path):
        """One failed probe is a blip — a restart, a GC pause, a slow query.
        Paging on it is how a pager gets muted."""
        log = install_fake_curl(tmp_path)
        result = run_probe(base_env(tmp_path, **DEAD))
        assert send_attempts(log) == 0, "one failure must not page"
        assert failure_count(tmp_path) == 1
        assert result.returncode == 0, (
            "a below-threshold blip must not fail the unit — a failed unit "
            "would fire its own OnFailure page and defeat the whole point"
        )

    def test_pages_once_the_threshold_of_consecutive_failures_is_reached(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, **DEAD)
        for expected in (0, 0, 1):
            run_probe(env)
            assert send_attempts(log) == expected
        assert failure_count(tmp_path) == 3

    def test_recovery_resets_the_counter(self, tmp_path: Path):
        """Down, down, UP, down, down: five cycles, two of them consecutive
        at the end — nobody gets paged."""
        log = install_fake_curl(tmp_path)
        for rc in ("1", "1", "0", "1", "1"):
            run_probe(base_env(tmp_path, FAKE_CURL_PROBE_RC=rc))
        assert send_attempts(log) == 0, "the recovery must have reset the counter"
        assert failure_count(tmp_path) == 2

    def test_threshold_is_configurable(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_LIVENESS_FAILURE_THRESHOLD="1", **DEAD)
        run_probe(env)
        assert send_attempts(log) == 1

    def test_a_longer_outage_keeps_paging_through_the_sender_dedup(self, tmp_path: Path):
        """Past the threshold the probe keeps calling the sender every cycle;
        the sender's own per-unit cooldown is the single source of dedup
        truth, so a sustained outage does not become a page storm."""
        log = install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_LIVENESS_FAILURE_THRESHOLD="1",
            ROBOTHOR_ALERT_COOLDOWN_SECONDS="3600",
            **DEAD,
        )
        for _ in range(4):
            run_probe(env)
        assert send_attempts(log) == 1, "the sender cooldown must dedup the sustained outage"


# ── what gets paged ──────────────────────────────────────────────────────────


class TestPageContent:
    def test_default_watched_unit_is_the_engine(self, tmp_path: Path):
        alert_log = install_recording_alert(tmp_path)
        install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_LIVENESS_FAILURE_THRESHOLD="1",
            ROBOTHOR_LIVENESS_ALERT_CMD=str(tmp_path / "bin" / "fake-alert.sh"),
            **DEAD,
        )
        env.pop("ROBOTHOR_LIVENESS_UNIT")
        result = run_probe(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "robothor-engine.service" in alert_log.read_text()

    def test_watched_unit_is_configurable(self, tmp_path: Path):
        alert_log = install_recording_alert(tmp_path)
        install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_LIVENESS_FAILURE_THRESHOLD="1",
            ROBOTHOR_LIVENESS_UNIT="robothor-bridge.service",
            ROBOTHOR_LIVENESS_ALERT_CMD=str(tmp_path / "bin" / "fake-alert.sh"),
            **DEAD,
        )
        run_probe(env)
        assert "robothor-bridge.service" in alert_log.read_text()


# ── what gets probed ─────────────────────────────────────────────────────────


class TestProbeEndpoint:
    def test_defaults_to_the_engines_unauthenticated_liveness_endpoint(self, tmp_path: Path):
        """`/live` is in the engine's PROBE_PATHS (robothor/engine/auth.py), so
        the watchdog needs no token — one less thing to be broken when the box
        is broken."""
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, **ALIVE)
        run_probe(env)
        assert "http://127.0.0.1:18800/live" in log.read_text()

    def test_engine_port_is_honored(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_ENGINE_PORT="19000", **ALIVE)
        run_probe(env)
        assert "http://127.0.0.1:19000/live" in log.read_text()

    def test_endpoint_is_configurable(self, tmp_path: Path):
        log = install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_LIVENESS_URL="http://127.0.0.1:9100/live",
            **ALIVE,
        )
        run_probe(env)
        assert "http://127.0.0.1:9100/live" in log.read_text()

    def test_probe_command_is_injectable(self, tmp_path: Path):
        """The probe command itself can be replaced — that is what makes this
        script testable without a live engine, and lets an instance probe
        something other than HTTP."""
        install_fake_curl(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_LIVENESS_PROBE_CMD="/bin/false")
        result = run_probe(env)
        assert result.returncode == 0
        assert failure_count(tmp_path) == 1

    def test_probe_bounds_its_own_wait(self, tmp_path: Path):
        """A wedged engine accepts the connection and never answers. Without a
        timeout the probe hangs and the watchdog is as dead as the thing it
        watches."""
        log = install_fake_curl(tmp_path)
        run_probe(base_env(tmp_path, ROBOTHOR_LIVENESS_TIMEOUT="7", **ALIVE))
        args = log.read_text().splitlines()
        assert "--max-time" in args
        assert "7" in args


# ── an undelivered page is not success (robothor/engine/alerts.py discipline) ─


class TestUndeliveredPageIsNotSuccess:
    def test_a_failed_send_fails_the_probe_loudly(self, tmp_path: Path):
        install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_LIVENESS_FAILURE_THRESHOLD="1",
            FAKE_CURL_PROBE_RC="1",
            FAKE_CURL_SEND_RC="1",
        )
        result = run_probe(env)
        assert result.returncode != 0, (
            "the sender's exit status must be checked, not assumed — "
            "`delivered = bool(sent)`, per robothor/engine/alerts.py"
        )
        assert "not delivered" in (result.stdout + result.stderr).lower(), (
            "the journal must say the page did not land, in those words"
        )
        assert failure_count(tmp_path) == 1, "the probe must still have run and counted"

    def test_a_failed_send_leaves_the_counter_armed(self, tmp_path: Path):
        """A page that did not land must not reset the count, or the next tick
        would start again from one and the outage would go unreported until it
        happened to last another full threshold."""
        log = install_fake_curl(tmp_path)
        env = base_env(tmp_path, **DEAD)  # threshold 3
        run_probe(env)
        run_probe(env)
        assert send_attempts(log) == 0

        broken = run_probe({**env, "FAKE_CURL_SEND_RC": "1"})
        assert broken.returncode != 0
        assert send_attempts(log) == 1, "the third consecutive failure must try to page"
        assert failure_count(tmp_path) == 3

        recovered = run_probe(env)
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        # Two sends on this tick, not one: the failed page was also spooled,
        # so the tick drains that copy (marked "⏳ DELAYED") and then raises
        # the fresh page. Duplication is the deliberate trade — for a pager,
        # one page twice beats one page never.
        assert send_attempts(log) == 3, "the next tick must retry the page immediately"


# ── unit templates ───────────────────────────────────────────────────────────


def unit_text(name: str) -> str:
    path = UNIT_DIR / name
    assert path.exists(), f"infra/systemd/{name} missing"
    return path.read_text()


def directives(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.lstrip().startswith(("#", ";"))]


class TestAlertUnitCannotSilenceItself:
    """The start limit turned a crash loop into an hour of silence."""

    def test_start_limit_is_disabled(self):
        lines = directives(unit_text("robothor-alert@.service"))
        assert "StartLimitIntervalSec=0" in lines, (
            "a crash-looping unit must not be able to rate-limit its own pager "
            "into 'start-limit-hit' silence (60 occurrences on 2026-08-20)"
        )
        for line in lines:
            if line.startswith("StartLimitIntervalSec="):
                assert line == "StartLimitIntervalSec=0"
            assert not line.startswith("StartLimitBurst="), (
                "StartLimitBurst is inert once the interval is 0 — leaving it "
                "invites someone to 'restore' the interval alongside it"
            )

    def test_the_inversion_is_documented_in_the_unit(self):
        """This is a deliberate inversion of the usual systemd advice. The next
        reader must find the reason in the file, not re-derive it from an
        incident."""
        text = unit_text("robothor-alert@.service").lower()
        assert "start-limit" in text or "start limit" in text
        assert "cooldown" in text, "the comment must point at the sender's own dedup"


class TestLivenessUnitsAreIndependentOfOnFailure:
    def test_service_and_timer_templates_exist(self):
        unit_text("robothor-liveness.service")
        unit_text("robothor-liveness.timer")

    def test_service_runs_the_probe_script_from_the_workspace(self):
        lines = directives(unit_text("robothor-liveness.service"))
        exec_lines = [line for line in lines if line.startswith("ExecStart=")]
        assert len(exec_lines) == 1, exec_lines
        assert "/opt/robothor/scripts/liveness_probe.sh" in exec_lines[0]

    def test_service_is_oneshot(self):
        assert "Type=oneshot" in directives(unit_text("robothor-liveness.service"))

    def test_service_does_not_depend_on_the_engine_it_watches(self):
        """A watchdog ordered after (or bound to) its target is not a watchdog:
        stop the engine and the probe stops with it."""
        text = "\n".join(directives(unit_text("robothor-liveness.service")))
        for directive in ("Requires=", "BindsTo=", "PartOf=", "After=", "Wants="):
            for line in text.splitlines():
                if line.startswith(directive):
                    assert "robothor-engine" not in line, (
                        f"{line!r} makes the watchdog depend on its own target"
                    )

    def test_service_allows_the_sender_its_full_retry_budget(self):
        """The sender retries for ~5 minutes through the boot DNS/secrets
        window. systemd's default TimeoutStartSec (90s) would SIGTERM it
        mid-retry and the page would be lost — the very bug this file exists
        to prevent."""
        lines = directives(unit_text("robothor-liveness.service"))
        timeouts = [line for line in lines if line.startswith("TimeoutStartSec=")]
        assert timeouts, "TimeoutStartSec must be set explicitly"
        seconds = int(re.sub(r"\D", "", timeouts[0]) or 0)
        assert seconds >= 600, f"{timeouts[0]} is shorter than the sender's retry budget"

    def test_service_loads_the_instance_env_and_optional_secrets(self):
        lines = directives(unit_text("robothor-liveness.service"))
        assert "EnvironmentFile=/etc/robothor/robothor.env" in lines
        assert "EnvironmentFile=-/run/robothor/secrets.env" in lines, (
            "the optional '-' prefix: /run is tmpfs and the file does not exist "
            "on a cold boot (tests/test_cold_boot.py)"
        )

    def test_service_pages_if_the_probe_itself_dies(self):
        assert "OnFailure=robothor-alert@%n.service" in directives(
            unit_text("robothor-liveness.service")
        )

    def test_timer_probes_at_least_every_ten_minutes(self):
        lines = directives(unit_text("robothor-liveness.timer"))
        active = [line for line in lines if line.startswith("OnUnitActiveSec=")]
        assert active, "OnUnitActiveSec must set the probe interval"
        assert "min" in active[0]
        assert int(re.sub(r"\D", "", active[0])) <= 10

    def test_timer_starts_probing_after_boot(self):
        lines = directives(unit_text("robothor-liveness.timer"))
        assert any(line.startswith("OnBootSec=") for line in lines), (
            "without OnBootSec the timer only starts counting from its first "
            "activation, so a box that boots into a dead engine says nothing"
        )

    def test_timer_is_installable(self):
        assert "WantedBy=timers.target" in directives(unit_text("robothor-liveness.timer"))


# ── shell hygiene ────────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_probe_script_is_shellcheck_clean():
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(PROBE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class TestFleetGuardWatchesSemanticsNotAliveness:
    """robothor-fleet-guard answers a different question than robothor-liveness.

    On 2026-08-23 the engine answered /live with a static 200 for 3h48m while
    its primary agent did not exist — a YAML typo had removed main.yaml from
    the fleet and the scheduler had pruned main's heartbeat and worker. A
    liveness probe cannot see that. /ready runs health.py's check_fleet, which
    returns 503 when a required agent is missing.
    """

    FLEET_GUARD = UNIT_DIR / "robothor-fleet-guard.service"
    FLEET_TIMER = UNIT_DIR / "robothor-fleet-guard.timer"

    def _env(self, text: str) -> dict[str, str]:
        return dict(
            line.split("=", 1)[1].split("=", 1)  # Environment=KEY=VALUE
            for line in text.splitlines()
            if line.startswith("Environment=") and "=" in line.split("=", 1)[1]
        )

    def test_the_units_exist(self):
        assert self.FLEET_GUARD.exists(), "robothor-fleet-guard.service missing"
        assert self.FLEET_TIMER.exists(), "robothor-fleet-guard.timer missing"

    def test_fleet_guard_probes_ready_not_live(self):
        """/live is a static 200 that a hollowed-out fleet passes happily."""
        env = self._env(self.FLEET_GUARD.read_text())
        url = env["ROBOTHOR_LIVENESS_URL"]
        assert url.endswith("/ready"), f"fleet guard must probe /ready, got {url}"

    def test_fleet_guard_has_an_independent_counter_and_cooldown(self):
        """liveness_probe.sh keys its failure counter — and
        send_failure_alert.sh keys its 1h cooldown — on the unit NAME. Sharing
        robothor-engine.service would let a database blip mute the fleet guard,
        and vice versa."""
        env = self._env(self.FLEET_GUARD.read_text())
        assert env["ROBOTHOR_LIVENESS_UNIT"] == "robothor-fleet-guard.service"
        assert env["ROBOTHOR_LIVENESS_STATE_DIR"] != "/run/robothor/liveness"

    def test_environment_overrides_come_after_the_environment_files(self):
        """systemd applies environment directives in order. An Environment=
        line placed before EnvironmentFile= is silently overridden by it."""
        lines = self.FLEET_GUARD.read_text().splitlines()
        last_file = max(i for i, l in enumerate(lines) if l.startswith("EnvironmentFile="))
        first_env = min(i for i, l in enumerate(lines) if l.startswith("Environment="))
        assert first_env > last_file, (
            "Environment= overrides must follow every EnvironmentFile= or "
            "robothor.env silently wins"
        )

    def test_fleet_guard_pages_on_its_own_failure(self):
        assert "OnFailure=robothor-alert@%n.service" in self.FLEET_GUARD.read_text()

    def test_fleet_guard_timer_probes_from_boot(self):
        """Without OnBootSec a box that boots into a broken fleet is never
        probed until someone activates the timer by hand."""
        text = self.FLEET_TIMER.read_text()
        assert "OnBootSec=" in text
        assert "OnUnitActiveSec=" in text


# ── the tick is also the spool drain ─────────────────────────────────────────
# The sender parks a page it could not deliver (DNS down: 63 `curl_rc=6` lines
# since 2026-08-31) in a durable spool. Something has to come back for it, and
# the callers that lose pages — cron-wrapper.sh, backup-offsite.sh,
# thermal-guard.sh, boot-guard.sh — have no retrying unit behind them. This
# timer does: it runs as root every 5 minutes, ordered After=network-online,
# which is exactly the shape a drain needs.


def spool_a_page(tmp_path: Path, text: str = "PAGE-SPOOLED", epoch: int = 1756000000) -> Path:
    spool = tmp_path / "alert-spool"
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / f"{epoch}-robothor-spooled.service.deadbeef.1.msg"
    path.write_text(text + "\n")
    return path


class TestTheTickDrainsTheAlertSpool:
    def test_a_healthy_tick_still_drains_a_spooled_page(self, tmp_path: Path):
        """The engine is fine, so nothing pages — but a page stranded by an
        earlier DNS outage must go out anyway. Without this the spool is only
        drained by the NEXT failure, which may be days away."""
        log = install_fake_curl(tmp_path)
        spooled = spool_a_page(tmp_path)
        result = run_probe(base_env(tmp_path, **ALIVE))
        assert result.returncode == 0, result.stdout + result.stderr
        assert send_attempts(log) == 1, "the tick did not drain the spool"
        assert "PAGE-SPOOLED" in log.read_text()
        assert not spooled.exists(), "a delivered spool file must be deleted"

    def test_a_drain_that_cannot_deliver_does_not_fail_the_tick(self, tmp_path: Path):
        """Telegram is still unreachable. The engine is healthy, so this tick
        has nothing to report — failing it would fire the probe's own
        OnFailure= page about a backlog that is simply still waiting."""
        install_fake_curl(tmp_path)
        spooled = spool_a_page(tmp_path)
        env = base_env(tmp_path, FAKE_CURL_SEND_RC="1", **ALIVE)
        result = run_probe(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert spooled.exists(), "an undelivered page must stay spooled"

    def test_the_drain_uses_the_sender_seam(self, tmp_path: Path):
        """One sender, one seam: the drain goes through
        ROBOTHOR_LIVENESS_ALERT_CMD like the page does, so an instance that
        overrides the sender does not silently lose the drain."""
        alert_log = install_recording_alert(tmp_path)
        install_fake_curl(tmp_path)
        env = base_env(
            tmp_path,
            ROBOTHOR_LIVENESS_ALERT_CMD=str(tmp_path / "bin" / "fake-alert.sh"),
            **ALIVE,
        )
        result = run_probe(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "--drain" in alert_log.read_text()
