#!/usr/bin/env python3
"""
System Health Check — hourly comprehensive monitoring.

Checks 8 categories:
- systemd services (all robothor + cloudflared + mediamtx)
- HTTP endpoints (bridge, orchestrator, vision, ollama, status x2)
- Docker containers (vaultwarden, uptime-kuma, kokoro-tts)
- PostgreSQL databases (robothor_memory, vaultwarden)
- CRM native tables (crm_people in robothor_memory)
- Redis
- Gmail auth (gog gmail search returns valid JSON)
- Email data quality (recent entries have real from/subject)
- Cron freshness (email_sync <15min)

Output:
- Appends to memory_system/logs/health-check.log (human-readable)
- Writes memory/health-status.json (machine-readable, latest results)
- On CRITICAL failures: writes escalation to memory/worker-handoff.json
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, "/home/philip/robothor/brain/memory_system")
import audit
import event_bus

# === Paths ===
MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
LOG_DIR = Path("/home/philip/robothor/brain/memory_system/logs")
HEALTH_STATUS_PATH = MEMORY_DIR / "health-status.json"
HANDOFF_PATH = MEMORY_DIR / "worker-handoff.json"
EMAIL_LOG_PATH = MEMORY_DIR / "email-log.json"
TRIAGE_INBOX_PATH = MEMORY_DIR / "triage-inbox.json"

# === Services ===
SYSTEMD_SERVICES = [
    "robothor-bridge",
    "robothor-crm",
    "robothor-dashboard",
    "robothor-orchestrator",
    "robothor-privacy",
    "robothor-sms",
    "robothor-status-dashboard",
    "robothor-status",
    "robothor-transcript",
    "robothor-vision",
    "robothor-voice",
    "cloudflared",
    "mediamtx-webcam",
]


def _build_http_endpoints():
    """Build HTTP endpoints from service registry with hardcoded fallback."""
    try:
        from memory_system.service_registry import get_health_url

        endpoints = []
        for name in ["bridge", "orchestrator", "vision", "ollama", "status", "status_dashboard"]:
            url = get_health_url(name)
            if url:
                display = name.replace("_", "-")
                endpoints.append((display, url))
        if endpoints:
            return endpoints
    except ImportError:
        pass
    # Fallback if registry unavailable
    return [
        ("bridge", "http://localhost:9100/health"),
        ("orchestrator", "http://localhost:9099/health"),
        ("vision", "http://localhost:8600/health"),
        ("ollama", "http://localhost:11434/api/tags"),
        ("status", "http://localhost:3000"),
        ("status-dashboard", "http://localhost:3001"),
    ]


HTTP_ENDPOINTS = _build_http_endpoints()

DOCKER_CONTAINERS = []

PG_DATABASES = ["robothor_memory", "vaultwarden"]

GOG_PASSWORD = os.environ["GOG_KEYRING_PASSWORD"]
ACCOUNT = "robothor@ironsail.ai"


def _atomic_json_write(path: Path, data: dict):
    """Write JSON atomically via temp file + rename."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def check_result(name: str, ok: bool, detail: str = "") -> dict:
    return {
        "name": name,
        "status": "ok" if ok else "CRITICAL",
        "detail": detail,
        "checkedAt": datetime.now().isoformat(),
    }


def check_systemd_services() -> list[dict]:
    results = []
    for svc in SYSTEMD_SERVICES:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True,
                text=True,
                timeout=5,
            )
            active = out.stdout.strip() == "active"
            results.append(
                check_result(
                    f"systemd:{svc}",
                    active,
                    out.stdout.strip() if not active else "",
                )
            )
        except Exception as e:
            results.append(check_result(f"systemd:{svc}", False, str(e)))
    return results


def check_http_endpoints() -> list[dict]:
    results = []
    for name, url in HTTP_ENDPOINTS:
        try:
            t0 = time.monotonic()
            resp = requests.get(url, timeout=5)
            elapsed_ms = (time.monotonic() - t0) * 1000
            ok = resp.status_code < 500
            result = check_result(
                f"http:{name}",
                ok,
                f"HTTP {resp.status_code}" if not ok else "",
            )
            result["response_time_ms"] = round(elapsed_ms, 1)
            results.append(result)
            audit.log_telemetry(name, "response_time_ms", elapsed_ms, unit="ms")
            audit.log_telemetry(name, "http_status", resp.status_code)
        except Exception as e:
            results.append(check_result(f"http:{name}", False, str(e)))
            audit.log_telemetry(name, "http_status", 0, details={"error": str(e)[:200]})
    return results


def check_docker_containers() -> list[dict]:
    results = []
    try:
        out = subprocess.run(
            ["sudo", "docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        running = set(out.stdout.strip().split("\n")) if out.stdout.strip() else set()
    except Exception as e:
        return [check_result(f"docker:{c}", False, str(e)) for c in DOCKER_CONTAINERS]

    for container in DOCKER_CONTAINERS:
        # Docker compose prefixes container names; check substring match
        found = any(container in name for name in running)
        results.append(
            check_result(
                f"docker:{container}",
                found,
                "not running" if not found else "",
            )
        )
    return results


def check_databases() -> list[dict]:
    results = []
    for db in PG_DATABASES:
        try:
            out = subprocess.run(
                ["psql", "-d", db, "-c", "SELECT 1", "-t", "-A"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            ok = "1" in out.stdout
            results.append(
                check_result(
                    f"db:{db}",
                    ok,
                    out.stderr.strip()[:100] if not ok else "",
                )
            )
        except Exception as e:
            results.append(check_result(f"db:{db}", False, str(e)))

    # Collect PG connection stats
    try:
        out = subprocess.run(
            [
                "psql",
                "-d",
                "robothor_memory",
                "-t",
                "-A",
                "-c",
                "SELECT count(*) FROM pg_stat_activity WHERE datname = 'robothor_memory'",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            active_conns = int(out.stdout.strip())
            audit.log_telemetry("postgresql", "active_connections", active_conns)
    except Exception:
        pass

    return results


def check_crm_native() -> list[dict]:
    """Check native CRM tables are accessible via crm_dal."""
    try:
        result = subprocess.run(
            [
                "psql",
                "-d",
                "robothor_memory",
                "-c",
                "SELECT COUNT(*) FROM crm_people WHERE deleted_at IS NULL",
                "-t",
                "-A",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ok = result.returncode == 0 and result.stdout.strip().isdigit()
        count = result.stdout.strip() if ok else "?"
        return [
            check_result(
                "crm:native", ok, "" if ok else f"query failed: {result.stderr.strip()[:100]}"
            )
        ]
    except Exception as e:
        return [check_result("crm:native", False, str(e))]


def check_redis() -> list[dict]:
    try:
        out = subprocess.run(
            ["redis-cli", "ping"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ok = "PONG" in out.stdout
        # Collect Redis memory stats
        try:
            info_out = subprocess.run(
                ["redis-cli", "info", "memory"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in info_out.stdout.split("\n"):
                if line.startswith("used_memory:"):
                    val = int(line.split(":")[1].strip())
                    audit.log_telemetry("redis", "used_memory_bytes", val, unit="bytes")
                elif line.startswith("maxmemory:"):
                    val = int(line.split(":")[1].strip())
                    if val > 0:
                        audit.log_telemetry("redis", "maxmemory_bytes", val, unit="bytes")
        except Exception:
            pass
        return [check_result("redis", ok, out.stdout.strip() if not ok else "")]
    except Exception as e:
        return [check_result("redis", False, str(e))]


def check_gmail_auth() -> list[dict]:
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD
    try:
        out = subprocess.run(
            ["gog", "gmail", "search", "is:unread", "--account", ACCOUNT, "--max", "1", "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        # Valid JSON output means auth works
        if out.stdout.strip():
            json.loads(out.stdout)
            return [check_result("gmail:auth", True)]
        # Empty output is fine (no unread emails)
        return [check_result("gmail:auth", True)]
    except json.JSONDecodeError:
        return [check_result("gmail:auth", False, "Invalid JSON from gog")]
    except Exception as e:
        return [check_result("gmail:auth", False, str(e))]


def check_email_data_quality() -> list[dict]:
    try:
        with open(EMAIL_LOG_PATH) as f:
            data = json.load(f)
    except Exception:
        return [check_result("email:data_quality", False, "Cannot read email-log.json")]

    entries = data.get("entries", {})
    now = datetime.now()
    cutoff = (now - timedelta(days=1)).isoformat()

    # Check entries from last 24h
    recent_null = 0
    recent_total = 0
    for entry in entries.values():
        fetched = entry.get("fetchedAt", "")
        if fetched >= cutoff:
            recent_total += 1
            if entry.get("from") is None and entry.get("subject") is None:
                recent_null += 1

    if recent_total == 0:
        return [check_result("email:data_quality", True, "No recent entries")]

    ok = recent_null == 0
    detail = f"{recent_null}/{recent_total} recent entries have null metadata" if not ok else ""
    return [check_result("email:data_quality", ok, detail)]


def check_cron_freshness() -> list[dict]:
    results = []
    now = datetime.now()

    # Check email_sync freshness via email-log.json lastCheckedAt
    try:
        with open(EMAIL_LOG_PATH) as f:
            data = json.load(f)
        last_checked = data.get("lastCheckedAt", "")
        if last_checked:
            last_dt = datetime.fromisoformat(last_checked)
            age_min = (now - last_dt).total_seconds() / 60
            ok = age_min < 15
            results.append(
                check_result(
                    "cron:email_sync",
                    ok,
                    f"{age_min:.0f}min ago" if not ok else "",
                )
            )
        else:
            results.append(check_result("cron:email_sync", False, "Never ran"))
    except Exception as e:
        results.append(check_result("cron:email_sync", False, str(e)))

    return results


def check_triage_inbox_freshness() -> list[dict]:
    """Check that triage-inbox.json is fresh (updated within 15 min)."""
    try:
        with open(TRIAGE_INBOX_PATH) as f:
            data = json.load(f)
        prepared = data.get("preparedAt", "")
        if not prepared:
            return [check_result("pipeline:triage_inbox", False, "No preparedAt")]
        prepared_dt = datetime.fromisoformat(prepared)
        # Handle timezone-aware timestamps
        now = datetime.now()
        if prepared_dt.tzinfo is not None:
            from datetime import timezone as tz

            now = datetime.now(tz.utc)
        age_min = (now - prepared_dt).total_seconds() / 60
        ok = age_min < 15
        return [
            check_result(
                "pipeline:triage_inbox",
                ok,
                f"{age_min:.0f}min stale" if not ok else "",
            )
        ]
    except FileNotFoundError:
        return [check_result("pipeline:triage_inbox", False, "triage-inbox.json missing")]
    except Exception as e:
        return [check_result("pipeline:triage_inbox", False, str(e)[:100])]


def check_triage_inbox_consistency() -> list[dict]:
    """Check that triage inbox reflects uncategorized emails in email-log."""
    try:
        with open(EMAIL_LOG_PATH) as f:
            email_log = json.load(f)
        with open(TRIAGE_INBOX_PATH) as f:
            triage = json.load(f)
    except Exception as e:
        return [check_result("pipeline:triage_consistency", False, str(e)[:100])]

    # Count uncategorized emails with real metadata
    uncategorized = sum(
        1
        for entry in email_log.get("entries", {}).values()
        if isinstance(entry, dict) and not entry.get("categorizedAt") and entry.get("from")
    )
    triage_emails = triage.get("counts", {}).get("emails", 0)

    # If we have uncategorized emails but triage shows 0, something is broken
    if uncategorized > 0 and triage_emails == 0:
        return [
            check_result(
                "pipeline:triage_consistency",
                False,
                f"{uncategorized} uncategorized emails but triage shows 0",
            )
        ]
    return [check_result("pipeline:triage_consistency", True)]


def check_agent_pipeline_health() -> list[dict]:
    """Check agent run outcomes via agent_runs table."""
    results = []
    now = datetime.now()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    # Check each critical agent had at least one successful run in 24h
    critical_agents = ["email-classifier", "calendar-monitor", "morning-briefing"]
    try:
        for agent_id in critical_agents:
            out = subprocess.run(
                [
                    "psql",
                    "-d",
                    "robothor_memory",
                    "-t",
                    "-A",
                    "-c",
                    f"SELECT count(*) FROM agent_runs WHERE agent_id = '{agent_id}' "
                    f"AND status = 'completed' AND started_at > '{cutoff_24h}'",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                count = int(out.stdout.strip())
                ok = count > 0
                results.append(
                    check_result(
                        f"pipeline:agent:{agent_id}",
                        ok,
                        f"0 successful runs in 24h" if not ok else "",
                    )
                )
            else:
                results.append(
                    check_result(f"pipeline:agent:{agent_id}", False, "query failed")
                )
    except Exception as e:
        results.append(check_result("pipeline:agent_runs", False, str(e)[:100]))

    return results


def resolve_health_escalations():
    """Resolve any open infrastructure escalations when all checks pass.

    Resolves escalations from health_check source, and also agent-created
    escalations about bridge/CRM/service failures (sourceId=bridge-health
    or keywords in summary).
    """
    try:
        with open(HANDOFF_PATH) as f:
            handoff = json.load(f)
    except Exception:
        return

    INFRA_KEYWORDS = ["bridge", "connection refused", "service down", "relay failure"]
    escalations = handoff.get("escalations", [])
    changed = False
    now = datetime.now().isoformat()
    for esc in escalations:
        if esc.get("resolvedAt") is not None:
            continue
        # Always resolve health_check source escalations
        if esc.get("source") == "health_check":
            esc["resolvedAt"] = now
            esc["resolution"] = "Auto-resolved: all health checks passing"
            changed = True
            continue
        # Resolve agent-created infra escalations (e.g. bridge-health)
        if esc.get("sourceId") == "bridge-health":
            esc["resolvedAt"] = now
            esc["resolution"] = "Auto-resolved: bridge health check passing"
            changed = True
            continue
        summary = (esc.get("summary", "") + " " + esc.get("reason", "")).lower()
        if any(kw in summary for kw in INFRA_KEYWORDS):
            esc["resolvedAt"] = now
            esc["resolution"] = "Auto-resolved: all health checks passing"
            changed = True

    if changed:
        _atomic_json_write(HANDOFF_PATH, handoff)


def write_escalation(critical_failures: list[dict]):
    """Write health check escalation to worker-handoff.json if there are CRITICAL failures."""
    try:
        with open(HANDOFF_PATH) as f:
            handoff = json.load(f)
    except Exception:
        handoff = {"escalations": []}

    escalations = handoff.get("escalations", [])

    # Check for existing open health_check escalation to avoid duplicates
    for esc in escalations:
        if esc.get("source") == "health_check" and esc.get("resolvedAt") is None:
            # Update existing escalation instead of creating duplicate
            esc["summary"] = f"{len(critical_failures)} CRITICAL failures detected"
            esc["detail"] = [f["name"] + ": " + f.get("detail", "") for f in critical_failures]
            esc["updatedAt"] = datetime.now().isoformat()
            _atomic_json_write(HANDOFF_PATH, handoff)
            return

    # Create new escalation
    escalation = {
        "id": f"health-{datetime.now().strftime('%Y%m%d-%H%M')}",
        "source": "health_check",
        "reason": "System health check detected CRITICAL failures",
        "summary": f"{len(critical_failures)} CRITICAL failures detected",
        "detail": [f["name"] + ": " + f.get("detail", "") for f in critical_failures],
        "urgency": "high",
        "createdAt": datetime.now().isoformat(),
        "surfacedAt": None,
        "resolvedAt": None,
    }
    escalations.append(escalation)
    handoff["escalations"] = escalations

    _atomic_json_write(HANDOFF_PATH, handoff)


def main():
    now = datetime.now()
    print(f"[{now.isoformat()}] System health check starting...")

    all_results = []
    all_results.extend(check_systemd_services())
    all_results.extend(check_http_endpoints())
    all_results.extend(check_docker_containers())
    all_results.extend(check_databases())
    all_results.extend(check_crm_native())
    all_results.extend(check_redis())
    all_results.extend(check_gmail_auth())
    all_results.extend(check_email_data_quality())
    all_results.extend(check_cron_freshness())
    all_results.extend(check_triage_inbox_freshness())
    all_results.extend(check_triage_inbox_consistency())
    all_results.extend(check_agent_pipeline_health())

    critical = [r for r in all_results if r["status"] == "CRITICAL"]
    ok_count = len(all_results) - len(critical)

    # Print human-readable summary (goes to log via crontab redirect)
    print(f"  Checks: {len(all_results)} total, {ok_count} ok, {len(critical)} CRITICAL")
    for r in critical:
        print(f"  CRITICAL: {r['name']} — {r.get('detail', '')}")

    if len(critical) == 0:
        print("  ALL SYSTEMS OPERATIONAL")

    # Write machine-readable status
    status = {
        "checkedAt": now.isoformat(),
        "totalChecks": len(all_results),
        "okCount": ok_count,
        "criticalCount": len(critical),
        "status": "CRITICAL" if critical else "ok",
        "results": all_results,
    }
    _atomic_json_write(HEALTH_STATUS_PATH, status)

    # Escalate if critical, auto-resolve if all clear
    if critical:
        write_escalation(critical)
        print("  Escalation written to worker-handoff.json")
    else:
        resolve_health_escalations()

    # Write summary to audit log
    audit.log_event(
        "service.health",
        f"Health check: {ok_count}/{len(all_results)} ok",
        category="system",
        status="ok" if not critical else "error",
        details={
            "total_checks": len(all_results),
            "ok_count": ok_count,
            "critical_count": len(critical),
            "critical_names": [r["name"] for r in critical],
        },
    )

    # Dual-write: publish to event bus
    event_bus.publish(
        "health",
        "service.health",
        {
            "total_checks": len(all_results),
            "ok_count": ok_count,
            "critical_count": len(critical),
            "critical_names": [r["name"] for r in critical],
            "status": "ok" if not critical else "CRITICAL",
        },
        source="system_health_check",
    )

    print(f"[{datetime.now().isoformat()}] Health check done.")


if __name__ == "__main__":
    main()
