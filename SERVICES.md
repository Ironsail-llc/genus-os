# Services — Operational Reference

Ports, endpoints and health checks for an instance installed on a host with
systemd. Every unit listed under "Platform units" has a template in
`infra/systemd/` and is installed by `scripts/install-units.sh` — never by hand.
A compose or Kubernetes instance runs the same processes without these units;
see `docs/deployment.md`.

## Platform units (need `sudo`)

All managed via `sudo systemctl {start,stop,restart,status} <unit>`.
Logs: `journalctl -u <unit> -f`

| Unit | Port | Working Dir | Description |
|------|------|-------------|-------------|
| robothor-vision.service | 8600 | workspace | Vision: smart detection (YOLO+InsightFace+alerts), modes: disarmed/basic/armed |
| robothor-orchestrator.service | 9099 | workspace | FastAPI RAG orchestrator + vision endpoints |
| robothor-bridge.service | 9100 | crm/bridge | Bridge: contact resolution, webhooks, CRM integration |
| robothor-secrets.service | — | scripts/ | Oneshot: writes `/run/robothor/secrets.env` (tmpfs, 0600) from the configured backend. Every service that needs a credential `Requires=` it |
| robothor-liveness.timer | — | scripts/ | Independent engine liveness watchdog: probes `/live` every 5min, pages via `send_failure_alert.sh` after 3 consecutive failures (covers SIGKILL, where OnFailure= never fires) |
| robothor-slo.timer | — | scripts/ | Reliability dead-man: hourly, pages on the AGE of the newest good backup (local dump 26h / offsite 26h / basebackup 8d) plus heartbeat delivery and LLM availability. Level-triggered, so a SKIPPED backup unit cannot go unnoticed (docs/runbooks/SLOS.md) |
| robothor-restore-drill.timer | — | scripts/ | Monthly restore drill: fetches the newest dump (offsite first), restores into a scratch DB, times and verifies it, drops it. NOT robothor-backup-verify, which only byte-compares (docs/runbooks/RESTORE_DRILL.md) |
| robothor-app.service | 3004 | app/ | Helm: Next.js 16 + Dockview live dashboard |
| robothor-engine.service | 18800 | workspace | Python Agent Engine: agents, channels, scheduler, hooks (Type=notify, WatchdogSec=90) |
| robothor-nats.service | 4222, 7422 | — | NATS server with JetStream: federation transport |
| robothor-xvfb.service | — | — | Virtual display server (Xvfb :99, 1280x1024) for computer use |
| robothor-vnc.service | 5900 | — | x11vnc server for monitoring virtual display (localhost only) |

`infra/systemd/` also ships the backup, basebackup, WAL-offsite, restore-drill,
thermal, fleet-guard, guardrail-watch, memory-eval and benchmark timers. They
are optional: enable the ones the deployment wants.

`genus doctor --only host.unit_drift` reports units installed on a box that no
template describes — the direction `install-units.sh` cannot see.

## Instance-only services (not shipped with the platform)

These units run on this deployment, but **no unit template ships in
`infra/systemd/`** for any of them, so a clean checkout cannot enable one.
Where the program itself lives varies: some is instance code under `brain/`,
`robothor-crm` wraps the Compose stack in `crm/`, and `mediamtx-webcam` runs
third-party software. They are listed so the information is not lost, not as
something a new instance can turn on.

| Unit | Port | Working Dir | Description |
|------|------|-------------|-------------|
| mediamtx-webcam.service | 8554, 8890 | — | USB webcam → RTSP + HLS stream (third-party MediaMTX) |
| robothor-voice.service | 8765 | instance `brain/voice-server` | Twilio voice: inbound ConversationRelay + outbound calling |
| robothor-sms.service | 8766 | instance `brain/sms-server` | Twilio SMS webhooks |
| robothor-transcript.service | — | instance `brain/memory_system` | Voice transcript watcher |
| robothor-crm.service | 3010, 8880 | `crm/` | Docker Compose wrapper: Uptime Kuma, Kokoro TTS |
| robothor-desktop.service | — | — | Openbox window manager on virtual display :99 |
| cloudflared.service | — | — | Third-party Cloudflare tunnel. An ingress pattern, not a shipped control |
| tailscaled.service | — | — | Third-party VPN |
| smbd.service / nmbd.service | 445, 137-138 | — | Third-party Samba file sharing |

The platform's own units are the ones with a template in `infra/systemd/`;
`docs/runbooks/INSTANCE_DOCTOR.md` and `scripts/install-units.sh` only know
about those.

## CLI Dependencies

| CLI | Install | Purpose |
|-----|---------|---------|
| `gog` | Go binary (`go install`) | Legacy Google Workspace CLI (Gmail, Calendar) — used via `exec` tool |
| `gws` | `npm install -g @googleworkspace/cli` | Google Workspace CLI v0.8+ — native engine tools (`gws_*`), MCP server for Claude Code |
| `gh` | `apt install gh` | GitHub CLI — used by `create_pull_request` tool |
| `nlm` | `pip install notebooklm-cli` | NotebookLM CLI — research notebooks |

## Health Checks

`genus doctor` answers this in one command, with severities and an exit code —
these are the raw probes behind the `services` category.

```bash
# The one command
genus doctor --category services

# Agent Engine
curl -s http://localhost:18800/health | jq .

# Bridge service
curl -s http://localhost:9100/health | jq .

# RAG orchestrator
curl -s http://localhost:9099/health | jq .

# Vision service
curl -s http://localhost:8600/health | jq .

# Helm dashboard
curl -s -o /dev/null -w "%{http_code}" http://localhost:3004/api/health && echo " OK"

# Readiness, which is what an orchestrator gates on
curl -s http://localhost:18800/ready | jq .

# Engine-served dashboards (status, ops, homepage, privacy)
curl -s http://localhost:18800/dashboards/status > /dev/null && echo "OK"

# Redis
redis-cli ping

# Ollama
curl -s http://localhost:11434/api/tags | jq '.models[].name'

# PostgreSQL
psql -d robothor_memory -c "SELECT count(*) FROM long_term_memory;" 2>/dev/null
```

## External Access (Cloudflare Tunnel)

An **instance** ingress pattern, shown as one deployment configured it. Nothing
here is a shipped control: the hostnames, the policies and which services are
exposed at all are the operator's, and `${INSTANCE_DOMAIN}` stands for whatever
domain that deployment uses.

| Hostname | Backend | Auth | Purpose |
|----------|---------|------|---------|
| cam.${INSTANCE_DOMAIN} | localhost:8890 | Cloudflare Access (email OTP) | Webcam HLS live stream |
| voice.${INSTANCE_DOMAIN} | localhost:8765 | Public | Twilio voice (inbound + outbound) |
| sms.${INSTANCE_DOMAIN} | localhost:8766 | Public | Twilio SMS webhook |
| engine.${INSTANCE_DOMAIN} | localhost:18800 | Cloudflare Access (email OTP) | Python Agent Engine |
| bridge.${INSTANCE_DOMAIN} | localhost:9100 | Cloudflare Access (email OTP) | Bridge service API |
| orchestrator.${INSTANCE_DOMAIN} | localhost:9099 | Cloudflare Access (email OTP) | RAG orchestrator API |
| vision.${INSTANCE_DOMAIN} | localhost:8600 | Cloudflare Access (email OTP) | Vision API |
| monitor.${INSTANCE_DOMAIN} | localhost:3010 | Cloudflare Access (email OTP) | Uptime Kuma monitoring |
| app.${INSTANCE_DOMAIN} | localhost:3004 | Cloudflare Access (email OTP) → app session | Helm — live dashboard |

The Helm dashboard trusts Cloudflare Access as its identity provider when
`CF_ACCESS_TEAM_DOMAIN` + `CF_ACCESS_AUD` are set: it verifies the
edge-injected `Cf-Access-Jwt-Assertion` (JWKS signature, issuer, audience) and
establishes its Auth.js session + bridge RBAC tokens from that identity — one
authentication at the edge, no second sign-in prompt.

All camera/vision ports (`8554`, `8889`, `8890`, `8600`) are bound to `127.0.0.1`. External access to the webcam is only possible through the Cloudflare tunnel with Zero Trust authentication.

## Engine API Endpoints (localhost:18800)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Daemon status, agent summary |
| GET | `/runs` | Recent agent runs (limit 20) |
| GET | `/costs?hours=24` | Per-agent cost breakdown |
| GET | `/costs/deep?hours=24` | RLM deep reasoning cost tracking |
| POST | `/chat/send` | SSE — chat with agent (delta, tool_start/end, done) |
| GET | `/chat/history` | Session message history |
| POST | `/chat/abort` | Cancel running response |
| POST | `/chat/clear` | Reset session history |
| POST | `/chat/plan/start` | SSE — plan mode exploration |
| POST | `/chat/plan/approve` | Approve pending plan |
| POST | `/chat/plan/reject` | Reject plan with feedback |
| GET | `/chat/plan/status` | Active plan state |
| POST | `/chat/deep/start` | SSE — deep reasoning (deep_start, deep_progress, deep_result, done) |
| GET | `/chat/deep/status` | Active deep session state |
| GET | `/api/runs/{id}/children` | Direct child runs of a parent |
| GET | `/api/runs/{id}/tree` | Full execution tree (recursive) |
| POST | `/api/runs/{id}/resume` | Resume from checkpoint |
| GET | `/api/v2/stats` | Guardrail events, budget exhaustions, checkpoints |
| GET | `/api/workflows` | Loaded workflow definitions |
| POST | `/api/workflows/{id}/execute` | Trigger workflow manually |

## Credentials

All services that need credentials load them from one file, written by one script:
- `ExecStartPre=$ROBOTHOR_WORKSPACE/scripts/load-secrets.sh` populates `/run/robothor/secrets.env`
- `EnvironmentFile=-/run/robothor/secrets.env` loads them into the service environment
- Services that load secrets: robothor-vision, robothor-orchestrator, robothor-bridge, robothor-engine, robothor-voice

`ROBOTHOR_SECRETS_BACKEND` decides where the values come from — `sops` (the age-encrypted
`/etc/robothor/secrets.enc.json`, via `scripts/decrypt-secrets.sh`), `file` (a plaintext
0600 file you manage) or `env` (already in the unit environment). Unset means auto-detect.
SOPS is opt-in; see `docs/deployment.md` § Secrets backends. In Python, read a credential
through `robothor.secrets.get_secret()`.

## System Crontab (instance)

The schedule below belongs to one deployment, not to the platform — a new
instance has none of it. View: `crontab -l` | Full reference:
`docs/CRON_MAP.md` (instance-local, not shipped)
Cron jobs that need credentials are wrapped with `scripts/cron-wrapper.sh` (sources `/run/robothor/secrets.env`).

| Schedule | Job | Log |
|----------|-----|-----|
| */5 * * * * | Calendar sync | memory_system/logs/calendar-sync.log |
| */5 * * * * | Email sync | memory_system/logs/email-sync.log |
| */30 6-22 * * 1-5 | Jira sync (M-F work hours) | memory_system/logs/jira-sync.log |
| */15 * * * * | Garmin health sync | memory_system/logs/garmin-sync.log |
| */10 * * * * | Continuous ingestion (Tier 1) | memory_system/logs/continuous-ingest.log |
| */10 * * * * | Meet transcript sync | memory_system/logs/meet-transcript-sync.log |
| 0 7,11,15,19 * * * | Periodic analysis (Tier 2) | memory_system/logs/periodic-analysis.log |
| 0 3 * * * | Memory maintenance (TTL, archival) | memory_system/logs/maintenance.log |
| 15 3 * * * | CRM consistency check | memory_system/logs/crm-consistency.log |
| 30 3 * * * | Intelligence pipeline (Tier 3) | memory_system/logs/intelligence.log |
| 0 4 * * * | Snapshot cleanup (>30 days) | — |
| 0 * * * * | System health check | memory_system/logs/health-check.log |
| 55 * * * * | Triage prep (hourly, prepares for next hour) | memory_system/logs/triage-prep.log |
| 10 * * * * | Triage cleanup (hourly, 10 min after Classifier) | memory_system/logs/triage-cleanup.log |
| 25 * * * * | Email response prep (hourly) | memory_system/logs/email-response-prep.log |
| */10 6-23 * * * | Supervisor relay | memory_system/logs/supervisor-relay.log |
| 0 6-22/4 * * * | Task cleanup (every 4h) | memory_system/logs/task-cleanup.log |
| 0 4 * * 0 | Data archival (Sunday) | memory_system/logs/data-archival.log |
| 30 4 * * * | SSD backup (daily, LUKS-encrypted) | ~/robothor/scripts/backup.log |
| 0 5 * * 0 | Weekly review (Sunday) | memory_system/logs/weekly-review.log |

## Engine Scheduled Agents (instance)

One deployment's fleet, as an example of what scheduling looks like. The
manifests are instance data and the models come from them — read the `model:`
blocks, never a number in this table.

View: `genus engine list` | Manifests: `docs/agents/*.yaml` (instance-local)

| Schedule | Job | Delivery |
|----------|-----|----------|
| 0 6-22 * * * | Email Classifier | announce → telegram |
| */15 6-22 * * * | Calendar Monitor | announce → telegram |
| 30 6-22 * * * | Email Analyst | announce → telegram |
| 45 6-22 * * * | Email Responder | announce → telegram |
| */17 6-22 * * * | Supervisor Heartbeat | announce → telegram |
| */10 * * * * | Vision Monitor | none (silent) |
| */30 6-22 * * * | Conversation Inbox Monitor | none (silent) |
| 0 6-22/2 * * * | Conversation Resolver | none (silent) |
| 0 10,18 * * * | CRM Steward | announce → telegram |
| 30 6 * * * | Morning Briefing | announce → telegram |
| 0 21 * * * | Evening Wind-Down | announce → telegram |

## Startup Order After Reboot

All services are system-level, enabled, and start automatically. If anything fails:

```bash
# 1. Ask the platform first: this is what genus doctor's services and host
#    categories are for.
genus doctor --category services

# 2. Then the units themselves. Add any instance-only unit from the section
#    above to this list if your deployment runs it.
for svc in robothor-secrets robothor-orchestrator \
  robothor-vision robothor-bridge robothor-app robothor-engine \
  robothor-nats robothor-xvfb robothor-vnc; do
  printf "%-35s %s\n" "$svc" "$(sudo systemctl is-active $svc)"
done

# 3. If orchestrator didn't start (depends on ollama + postgres)
sudo systemctl restart robothor-orchestrator

# 4. A failed robothor-secrets.service takes four services down with it and
#    nothing retries a oneshot. journalctl -u robothor-secrets says which
#    backend it chose and why it refused.
sudo systemctl status robothor-secrets
```
