"""The box must survive a cold boot — and page the operator if it doesn't.

Both of these were found by actually rebooting the machine (2026-07-14), not by
reading the units.

1. EVERY service failed on the first boot attempt.

   The units carry two environment files:

       EnvironmentFile=/etc/robothor/robothor.env
       ExecStartPre=${ROBOTHOR_WORKSPACE}/scripts/decrypt-secrets.sh
       EnvironmentFile=/run/robothor/secrets.env      <- created BY the ExecStartPre

   systemd loads *all* EnvironmentFile= directives before it runs ExecStartPre.
   `/run` is tmpfs, so on a cold boot `secrets.env` does not exist yet — and a
   non-optional EnvironmentFile that is missing fails the unit outright:

       robothor-engine.service: Failed to load environment files: No such file or directory
       robothor-engine.service: Failed to spawn 'start-pre' task: No such file or directory

   `Restart=always` papered over it for the long-running services. The `oneshot`
   delphi units have no Restart and simply did not run.

   The fix is the `-` prefix, which marks the file optional. The repo already had
   it; the installed units had drifted.

2. THE PAGER COULD NOT PAGE — at exactly the moment it was needed.

   `send_failure_alert.sh` reads its Telegram token from the environment, and the
   token lives ONLY in `/run/robothor/secrets.env`. At boot that file does not
   exist yet, so every OnFailure alert died with:

       send_failure_alert: ROBOTHOR_TELEGRAM_BOT_TOKEN is not set

   Five services failed on this boot and the operator was told nothing. An alert
   that is silent during a boot failure is worse than no alert: it is a boot
   failure you never hear about.

   The alert unit runs as root and the age key is root-readable, so the sender can
   decrypt its own secrets. It must.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SENDER = REPO_ROOT / "scripts" / "send_failure_alert.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"


def _units_with_secrets_env() -> list[Path]:
    return [u for u in UNIT_DIR.glob("*.service") if "secrets.env" in u.read_text()]


class TestUnitsSurviveAColdBoot:
    def test_there_are_units_to_check(self) -> None:
        assert _units_with_secrets_env(), "expected shipped units that load secrets.env"

    def test_secrets_env_is_optional_in_every_unit(self) -> None:
        """`/run` is tmpfs: secrets.env does not exist until ExecStartPre creates it."""
        offenders = [
            u.name
            for u in _units_with_secrets_env()
            if re.search(
                r"^EnvironmentFile=/run/robothor/secrets\.env", u.read_text(), re.MULTILINE
            )
        ]
        assert not offenders, (
            f"{offenders} load /run/robothor/secrets.env without the '-' prefix. "
            "systemd loads every EnvironmentFile BEFORE ExecStartPre — which is what "
            "creates that file — so these units fail on every cold boot."
        )


class TestThePagerCanPageDuringABootFailure:
    def test_sender_recovers_the_secrets_itself(self) -> None:
        """The token lives only in tmpfs, which is empty exactly when a boot fails."""
        body = SENDER.read_text()
        assert "decrypt-secrets.sh" in body or "secrets.env" in body, (
            "send_failure_alert.sh reads its token from the environment, but the token "
            "lives ONLY in /run/robothor/secrets.env — which does not exist at boot. "
            "So the pager is silent precisely when a service fails to start. It must "
            "source (or decrypt) the secrets itself before giving up."
        )

    def test_sender_still_fails_loudly_when_it_truly_cannot_get_a_token(self) -> None:
        """Recovering the secrets must not turn a real misconfiguration into silence."""
        body = SENDER.read_text()
        assert "is not set" in body and "exit 1" in body, (
            "the sender must still exit non-zero when no token can be obtained — "
            "a pager that swallows its own failure is the bug we keep finding"
        )
