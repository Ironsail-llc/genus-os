#!/usr/bin/env python3
"""supervisor_relay.py — Mechanical relay for meetings and system health.

Writes stale-worker and CRM-health alerts to worker-handoff.json for the
supervisor to investigate and surface. Only sends directly to Telegram for
time-critical meeting alerts (within 20 min).
"""

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

import requests

# Event bus for real-time event consumption
sys.path.insert(0, os.path.expanduser("~/clawd"))
try:
    from memory_system import event_bus
except ImportError:
    event_bus = None

logger = logging.getLogger(__name__)

MEMORY_DIR = os.path.expanduser("~/clawd/memory")
HANDOFF_PATH = os.path.join(MEMORY_DIR, "worker-handoff.json")
CALENDAR_PATH = os.path.join(MEMORY_DIR, "calendar-log.json")
JIRA_LOG_PATH = os.path.join(MEMORY_DIR, "jira-log.json")
RELAY_STATE_PATH = os.path.join(MEMORY_DIR, "relay-state.json")
TRIAGE_STATUS_PATH = os.path.join(MEMORY_DIR, "triage-status.md")

# Telegram config (only used for meeting alerts)
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PHILIP_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7636850023")

# Thresholds
STALE_WORKER_MINUTES = 35
MEETING_ALERT_MINUTES = 20
STALE_COOLDOWN_MINUTES = 60
CRM_COOLDOWN_MINUTES = 30


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_telegram(text):
    """Send a message to Philip via Telegram. Only for meeting alerts."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": PHILIP_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        if resp.ok:
            print(f"relay: sent meeting alert to Telegram ({len(text)} chars)")
        else:
            print(f"relay: Telegram API error: {resp.status_code}")
    except Exception as e:
        print(f"relay: Telegram send failed: {e}")


def is_waking_hours():
    """Check if it's between 7 AM and 10 PM ET (DST-aware)."""
    from zoneinfo import ZoneInfo

    now_et = datetime.now(ZoneInfo("America/New_York"))
    return 7 <= now_et.hour <= 22


def cooldown_ok(relay_state, key, cooldown_minutes):
    """Check if enough time has passed since last alert of this type."""
    last_sent = relay_state.get(key, "")
    if not last_sent:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sent)
        elapsed = (datetime.now(UTC) - last_dt).total_seconds() / 60
        return elapsed >= cooldown_minutes
    except (ValueError, TypeError):
        return True


def add_escalation_to_handoff(handoff, summary, urgency="medium"):
    """Add an escalation dict to handoff for supervisor to investigate."""
    escalations = handoff.setdefault("escalations", [])
    escalations.append(
        {
            "source": "relay",
            "summary": summary,
            "urgency": urgency,
            "createdAt": datetime.now(UTC).isoformat(),
            "surfacedAt": None,
        }
    )


def get_worker_last_run():
    """Get worker last run time from triage-status.md or handoff."""
    # Try triage-status.md first
    try:
        with open(TRIAGE_STATUS_PATH) as f:
            for line in f:
                if line.startswith("**Last run:**"):
                    ts = line.split("**Last run:**")[1].strip()
                    return datetime.fromisoformat(ts)
    except (FileNotFoundError, ValueError, IndexError):
        pass

    # Fall back to handoff lastRunAt
    handoff = load_json(HANDOFF_PATH)
    last_run = handoff.get("lastRunAt", "")
    if last_run:
        try:
            return datetime.fromisoformat(last_run)
        except (ValueError, TypeError):
            pass
    return None


def check_stale_worker(handoff, relay_state):
    """Check if triage worker is stale. Write to handoff if so."""
    last_dt = get_worker_last_run()

    if last_dt is None:
        if cooldown_ok(relay_state, "stale_sent", STALE_COOLDOWN_MINUTES):
            relay_state["stale_sent"] = datetime.now(UTC).isoformat()
            add_escalation_to_handoff(handoff, "Worker has never run", "high")
            return True
        return False

    now = datetime.now(UTC)
    delta_min = (now - last_dt).total_seconds() / 60

    if delta_min > STALE_WORKER_MINUTES:
        if cooldown_ok(relay_state, "stale_sent", STALE_COOLDOWN_MINUTES):
            relay_state["stale_sent"] = datetime.now(UTC).isoformat()
            add_escalation_to_handoff(
                handoff,
                f"Worker stale — last ran {int(delta_min)} min ago",
                "medium",
            )
            return True
    else:
        # Worker is healthy — clear cooldown
        relay_state.pop("stale_sent", None)

    return False


def check_upcoming_meetings(calendar_data):
    """Check for meetings within the alert window. These go DIRECTLY to Telegram."""
    alerts = []
    now_epoch = int(time.time())
    now_iso = datetime.now(UTC).isoformat()

    for meeting in calendar_data.get("meetings", []):
        if meeting.get("notifiedAt"):
            continue

        start_epoch = meeting.get("startEpoch")
        if not start_epoch:
            continue

        minutes_until = (start_epoch - now_epoch) / 60

        if 0 < minutes_until <= MEETING_ALERT_MINUTES:
            title = meeting.get("title", "Unknown meeting")
            alerts.append(f"\u23f0 {title} \u2014 {int(minutes_until)} min")
            meeting["notifiedAt"] = now_iso

    if alerts:
        save_json(CALENDAR_PATH, calendar_data)

    return alerts


def _check_health_via_event_bus():
    """Check latest health event from event bus. Returns (ok, msg) or None if unavailable."""
    if event_bus is None or not event_bus.EVENT_BUS_ENABLED:
        return None
    try:
        recent = event_bus.read_recent("health", count=1)
        if not recent:
            return None
        event = recent[0]
        payload = event.get("payload", {})
        status = payload.get("status", "unknown")
        if status == "ok":
            return (True, "ok")
        return (False, f"CRM Bridge degraded: {status}")
    except Exception as e:
        logger.debug("Event bus health check failed: %s", e)
        return None


def check_crm_health(handoff, relay_state):
    """Check CRM bridge health. Try event bus first, fall back to HTTP."""
    # Try event bus first (faster, no HTTP overhead)
    bus_result = _check_health_via_event_bus()
    if bus_result is not None:
        ok, msg = bus_result
        if ok:
            relay_state.pop("crm_down_sent", None)
            return False
    else:
        # Fall back to HTTP health check
        try:
            _bridge_health = None
            try:
                from memory_system.service_registry import get_health_url

                _bridge_health = get_health_url("bridge")
            except ImportError:
                pass
            resp = requests.get(_bridge_health or "http://localhost:9100/health", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    relay_state.pop("crm_down_sent", None)
                    return False
                msg = f"CRM Bridge degraded: {data.get('status')}"
            else:
                msg = f"CRM Bridge HTTP {resp.status_code}"
        except Exception:
            msg = "CRM Bridge unreachable"

    if cooldown_ok(relay_state, "crm_down_sent", CRM_COOLDOWN_MINUTES):
        relay_state["crm_down_sent"] = datetime.now(UTC).isoformat()
        add_escalation_to_handoff(handoff, msg, "high")
        return True
    return False


def check_new_jira():
    """Send Telegram alerts for new Jira tickets. Returns count of alerts sent."""
    jira_log = load_json(JIRA_LOG_PATH)
    pending = jira_log.get("pendingActions", [])

    unsurfaced = [a for a in pending if a.get("surfacedAt") is None]
    if not unsurfaced:
        return 0

    now_iso = datetime.now(UTC).isoformat()
    sent = 0
    for action in unsurfaced:
        ticket = action.get("ticket", "???")
        summary = action.get("summary", "")
        # Look up priority from activeTickets
        active = jira_log.get("activeTickets", {}).get(ticket, {})
        priority = active.get("priorityMapped", "")
        url = active.get("url", "")

        msg = f"\U0001f3ab New Jira: {ticket} \u2014 {summary}"
        if priority:
            msg += f" ({priority})"
        if url:
            msg += f"\n{url}"

        send_telegram(msg)
        action["surfacedAt"] = now_iso
        sent += 1

    if sent:
        save_json(JIRA_LOG_PATH, jira_log)
        print(f"relay: sent {sent} Jira alert(s) to Telegram")

    return sent


def main():
    handoff = load_json(HANDOFF_PATH)
    calendar_data = load_json(CALENDAR_PATH)
    relay_state = load_json(RELAY_STATE_PATH)

    wrote_handoff = False

    # 1. Meeting alerts — go DIRECTLY to Telegram (can't wait for hourly supervisor)
    meeting_alerts = check_upcoming_meetings(calendar_data)
    if meeting_alerts:
        send_telegram("\n".join(meeting_alerts))

    # 1b. New Jira tickets — go DIRECTLY to Telegram
    jira_alerts = check_new_jira()

    # 2. Stale worker — write to handoff (only during waking hours)
    if is_waking_hours():
        if check_stale_worker(handoff, relay_state):
            wrote_handoff = True

    # 3. CRM health — write to handoff (only during waking hours)
    if is_waking_hours():
        if check_crm_health(handoff, relay_state):
            wrote_handoff = True

    # Save relay state (cooldown tracking)
    save_json(RELAY_STATE_PATH, relay_state)

    # Save handoff if we added escalations
    if wrote_handoff:
        save_json(HANDOFF_PATH, handoff)
        print("relay: wrote escalations to handoff")
    elif not meeting_alerts and not jira_alerts:
        print("relay: nothing to surface")


if __name__ == "__main__":
    main()
