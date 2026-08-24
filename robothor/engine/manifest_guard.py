"""Page the operator when an agent manifest stops parsing.

WHY THIS EXISTS

On 2026-08-23 at 09:38 a YAML indentation error went into
``docs/agents/main.yaml``. The engine stayed ``active (running)`` and perfectly
healthy, so systemd's ``OnFailure=`` never fired. The loader dropped the
unreadable file, and five minutes later ``reconcile_schedules`` — unable to
tell "broken" from "deliberately deleted" — DELETED the primary agent's
heartbeat and worker schedules. The operator sent four Telegram messages over
3h48m and got total silence. The parse error was logged 109 times and reached
nobody.

Four independent controls that should have caught it were inert: the fleet
guard in ``/ready`` was built but disabled, ``robothor-liveness.timer`` was
merged but never installed, ``validate_agents.py`` skips gitignored (i.e. every
real) manifest, and the uptime monitor was pointed at an authenticated path.
This module is the in-band half of the replacement.

WHY ``critical``

``alerts._PAGE_LEVELS`` is ``{"critical"}``. Anything softer becomes a
``crm_agent_notifications`` row addressed ``to_agent="main"``, read by main's
heartbeat — and a manifest-parse failure is structurally liable to be ABOUT
main, whose heartbeat has just been deleted and whose config will not load. A
``warning`` here files the alarm in the corpse's inbox.

Fatigue is therefore controlled by the FLOOR, not by softening the level.

There is deliberately no enable/disable flag. A kill switch on a control that
exists because a control was silent is how the next incident happens. The floor
is the pressure valve: set ``ROBOTHOR_MANIFEST_ALERT_DEDUP`` higher.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.engine.alerts import alert

if TYPE_CHECKING:
    from robothor.engine.config import ManifestScan

logger = logging.getLogger(__name__)

#: Seconds before the same set of broken files pages again. One hour, not
#: model_breaker's six: a broken manifest is operator-fixable in about sixty
#: seconds and reconcile re-checks every five minutes, so an hourly nag against
#: an outage that is still open is proportionate. model_breaker's floor is
#: calibrated for a provider outage the operator cannot do anything about.
ALERT_DEDUP_SECONDS = int(os.environ.get("ROBOTHOR_MANIFEST_ALERT_DEDUP", "3600"))


def _state_path() -> Path:
    """Where the last-alerted stamps live.

    Under /run (tmpfs) on purpose: a reboot re-arms the guard, which is the
    behaviour you want from something this important.
    """
    return Path(
        os.environ.get("ROBOTHOR_MANIFEST_GUARD_STATE", "/run/robothor/manifest-guard-alerts.json")
    )


def _load_state() -> dict[str, float]:
    """Read {key: last_alerted_epoch}. Missing or corrupt → empty (fail open).

    Fail open because a guard that cannot read its own dedup file should page
    twice, never zero times.
    """
    try:
        raw = json.loads(_state_path().read_text())
        if isinstance(raw, dict):
            return {str(k): float(v) for k, v in raw.items()}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("manifest guard state unreadable (%s) — treating as empty", exc)
    return {}


def _save_state(state: dict[str, float]) -> None:
    """Persist stamps via an atomic swap. Best-effort; never blocks a page."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except Exception as exc:
        logger.warning("manifest guard could not persist alert dedup state: %s", exc)


def _scan_key(scan: ManifestScan) -> str:
    """Dedup key = WHICH files are broken, not merely THAT something is.

    So the same breakage pages once per floor, but a second file breaking is a
    new key and pages immediately — that is an escalation, not a repeat.
    """
    if not scan.dir_readable:
        return "dir-unreadable"
    names = ",".join(sorted(f.filename for f in scan.failures))
    return hashlib.sha256(names.encode()).hexdigest()[:16]


def _body(scan: ManifestScan, context: str) -> str:
    lines = [f"Context: {context}"]
    if not scan.dir_readable:
        lines.append("The manifest directory is UNREADABLE — the fleet cannot be enumerated.")
    else:
        lines.append(f"{len(scan.failures)} manifest(s) could not be parsed:")
        lines += [f"  - {f.filename}: {f.error_type} — {f.detail}" for f in scan.failures]
        lines.append(f"{len(scan.manifests)} manifest(s) still load; {scan.scanned} scanned.")
    # The operator's first question, answered before they have to ask it.
    lines.append("")
    lines.append("No schedules were pruned — reconcile refuses to delete from an incomplete read.")
    lines.append("Fix the file; the next reconcile (<=5 min) picks it up automatically.")
    return "\n".join(lines)


async def alert_manifest_scan(scan: ManifestScan, *, context: str) -> None:
    """Page on a dirty scan; send a recovery notice when it goes clean again.

    Never raises: an alerting failure must not stop reconciliation. Same
    reasoning as ``scheduler._alert_workflow_cron_failure``.
    """
    try:
        now = time.time()
        state = _load_state()

        if scan.clean:
            # Recovery: tell the operator it noticed, without interrupting them.
            if state:
                _save_state({})
                await alert(
                    "info",
                    "Agent manifests parse again",
                    f"Context: {context}\nAll {len(scan.manifests)} manifest(s) load. "
                    "Schedules reconcile normally from here.",
                )
            return

        key = _scan_key(scan)
        last = state.get(key)
        if last is not None and (now - last) < ALERT_DEDUP_SECONDS:
            logger.info(
                "manifest guard: %d manifest(s) still failing, within the %ds alert floor",
                len(scan.failures),
                ALERT_DEDUP_SECONDS,
            )
            return

        title = (
            "Manifest directory unreadable"
            if not scan.dir_readable
            else f"{len(scan.failures)} agent manifest(s) failed to parse"
        )
        # Checked, not assumed — the discipline from alerts.py, where assuming a
        # send worked hid an arity bug while 432+ alerts went nowhere. An
        # undelivered page leaves the key unstamped so the next tick retries.
        delivered = await alert("critical", title, _body(scan, context))
        if delivered:
            _save_state({**state, key: now})
        else:
            logger.error("manifest guard: page was NOT delivered — will retry next tick")
    except Exception as exc:
        logger.warning("manifest guard: alerting failed (%s) — reconciliation continues", exc)
