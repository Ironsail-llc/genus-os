# Services — Operational Reference

## System-Level Services (need `sudo`)

All managed via `sudo systemctl {start,stop,restart,status} <unit>`.
Logs: `journalctl -u <unit> -f`

| Unit | Port | Working Dir | Description |
|------|------|-------------|-------------|
| robothor-vision.service | 8600 | brain/memory_system | Vision: smart detection (YOLO+InsightFace+Telegram alerts), modes: disarmed/basic/armed |
| mediamtx-webcam.service | 8554, 8890 | — | USB webcam → RTSP + HLS stream |
| robothor-orchestrator.service | 9099 | brain/memory_system | FastAPI RAG orchestrator + vision endpoints |
| robothor-voice.service | 8765 | brain/voice-server | Twilio ConversationRelay voice |
| robothor-sms.service | 8766 | brain/sms-server | Twilio SMS webhooks |
| robothor-status.service | 3000 | brain/robothor-status | robothor.ai homepage |
| robothor-status-dashboard.service | 3001 | brain/robothor-status-dashboard | status.robothor.ai dashboard |
| robothor-dashboard.service | 3003 | brain/dashboard | Ops dashboard (ops.robothor.ai) |
| robothor-privacy.service | 3002 | brain/privacy-policy | Privacy policy (privacy.robothor.ai) |
| robothor-transcript.service | — | brain/memory_system | Voice transcript watcher |
| robothor-crm.service | 3010, 8222, 8880 | crm/ | Docker Compose: Vaultwarden, Uptime Kuma, Kokoro TTS (3 containers) |
| robothor-bridge.service | 9100 | crm/bridge | Bridge: contact resolution, webhooks, CRM integration |
| bridge-watchdog.timer | — | scripts/ | Self-healing watchdog: checks bridge every 5min, auto-restarts on 2 failures |
| robothor-app.service | 3004 | app/ | Helm: Next.js 16 + Dockview live dashboard (app.robothor.ai) |
| smbd.service | 445 | — | Samba file sharing (local network + Tailscale only) |
| nmbd.service | 137-138 | — | NetBIOS name service for Samba |
| moltbot-gateway.service | 18789 | ~/moltbot | OpenClaw messaging gateway |
| cloudflared.service | — | — | Cloudflare tunnel (robothor.ai) |
| tailscaled.service | — | — | Tailscale VPN (ironsail tailnet) |

## Health Checks

```bash
# Vision service
curl -s http://localhost:8600/health | jq .

# RAG orchestrator
curl -s http://localhost:9099/health | jq .

# Status server
curl -s http://localhost:3000 > /dev/null && echo "OK"

# Status dashboard
curl -s http://localhost:3001 > /dev/null && echo "OK"

# MediaMTX RTSP (test frame capture)
ffmpeg -rtsp_transport tcp -i rtsp://localhost:8554/webcam -frames:v 1 -y /tmp/test.jpg 2>/dev/null && echo "OK"

# MediaMTX HLS (test stream)
curl -s -o /dev/null -w "%{http_code}" http://localhost:8890/webcam/ && echo " OK"

# Webcam via Cloudflare tunnel (requires Cloudflare Access auth)
# Visit: https://cam.robothor.ai/webcam/

# Ops dashboard
curl -s http://localhost:3003 > /dev/null && echo "OK"

# Privacy policy
curl -s http://localhost:3002 > /dev/null && echo "OK"

# Moltbot gateway
curl -s http://localhost:18789/health 2>/dev/null || echo "No health endpoint — check systemctl"

# Bridge service
curl -s http://localhost:9100/health | jq .

# Helm (business layer app)
curl -s -o /dev/null -w "%{http_code}" http://localhost:3004/api/health && echo " OK"

# Vaultwarden
curl -s -o /dev/null -w "%{http_code}" http://localhost:8222 && echo " OK"

# Uptime Kuma
curl -s -o /dev/null -w "%{http_code}" http://localhost:3010 && echo " OK"

# Redis
redis-cli ping

# Cloudflare tunnel
curl -s https://robothor.ai > /dev/null && echo "OK"

# Samba
smbclient -L //localhost -U philip%$SAMBA_PASSWORD -N 2>/dev/null | grep robothor

# Tailscale
tailscale status | head -3

# Ollama
curl -s http://localhost:11434/api/tags | jq '.models[].name'

# PostgreSQL
psql -d robothor_memory -c "SELECT count(*) FROM long_term_memory;" 2>/dev/null
```

## External Access (Cloudflare Tunnel)

| Hostname | Backend | Auth | Purpose |
|----------|---------|------|---------|
| cam.robothor.ai | localhost:8890 | Cloudflare Access (email OTP) | Webcam HLS live stream |
| robothor.ai | localhost:3000 | Public | Homepage |
| status.robothor.ai | localhost:3001 | Public | Status dashboard |
| dashboard.robothor.ai | localhost:3001 | Public | Dashboard (alias) |
| privacy.robothor.ai | localhost:3002 | Public | Privacy policy |
| ops.robothor.ai | localhost:3003 | Cloudflare Access (email OTP) | Ops dashboard |
| voice.robothor.ai | localhost:8765 | Public | Twilio voice |
| sms.robothor.ai | localhost:8766 | Public | Twilio SMS webhook |
| gateway.robothor.ai | localhost:18789 | Cloudflare Access (email OTP) | OpenClaw gateway |
| bridge.robothor.ai | localhost:9100 | Cloudflare Access (email OTP) | Bridge service API |
| orchestrator.robothor.ai | localhost:9099 | Cloudflare Access (email OTP) | RAG orchestrator API |
| vision.robothor.ai | localhost:8600 | Cloudflare Access (email OTP) | Vision API |
| monitor.robothor.ai | localhost:3010 | Cloudflare Access (email OTP) | Uptime Kuma monitoring |
| vault.robothor.ai | localhost:8222 | Cloudflare Access (email OTP) | Vaultwarden password vault |
| app.robothor.ai | localhost:3004 | Cloudflare Access (email OTP) | Helm — live dashboard |

All camera/vision ports (`8554`, `8889`, `8890`, `8600`) are bound to `127.0.0.1`. External access to the webcam is only possible through the Cloudflare tunnel with Zero Trust authentication.

## Credentials

All services that need credentials use SOPS+age decryption:
- `ExecStartPre=/home/philip/robothor/scripts/decrypt-secrets.sh` decrypts secrets to `/run/robothor/secrets.env`
- `EnvironmentFile=/run/robothor/secrets.env` loads them into the service environment
- Services with SOPS injection: robothor-vision, robothor-orchestrator, robothor-bridge, moltbot-gateway

## System Crontab

View: `crontab -l` | Full reference: `docs/CRON_MAP.md`
Cron jobs that need credentials are wrapped with `scripts/cron-wrapper.sh` (sources `/run/robothor/secrets.env`).

| Schedule | Job | Log |
|----------|-----|-----|
| */5 * * * * | Calendar sync | memory_system/logs/calendar-sync.log |
| */5 * * * * | Email sync | memory_system/logs/email-sync.log |
| */30 6-22 * * 1-5 | Jira sync (M-F work hours) | memory_system/logs/jira-sync.log |
| */15 * * * * | Garmin health sync | ~/garmin-sync/sync.log |
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
| 0 4 * * 0 | Data archival (Sunday) | memory_system/logs/data-archival.log |
| 30 4 * * * | SSD backup (daily, LUKS-encrypted) | ~/robothor/scripts/backup.log |
| 0 5 * * 0 | Weekly review (Sunday) | memory_system/logs/weekly-review.log |

## OpenClaw Cron Jobs

View: `cat ~/.openclaw/cron/jobs.json` | Model: **Kimi K2.5** (Opus 4.6 fallback)

| Schedule | Job | Delivery |
|----------|-----|----------|
| 0 6-22 * * * | Email Classifier | none (silent) |
| */15 6-22 * * * | Calendar Monitor | none (silent) |
| 30 6-22 * * * | Email Analyst | none (silent) |
| 45 6-22 * * * | Email Responder | none (silent) |
| */17 6-22 * * * | Supervisor Heartbeat | announce → telegram |
| */10 * * * * | Vision Monitor | none (silent) |
| */30 6-22 * * * | Conversation Inbox Monitor | none (silent) |
| 0 6-22/2 * * * | Conversation Resolver | none (silent) |
| 0 10,18 * * * | CRM Steward | none (silent) |
| 30 6 * * * | Morning Briefing | announce → telegram |
| 0 21 * * * | Evening Wind-Down | announce → telegram |

## Startup Order After Reboot

All services are system-level, enabled, and start automatically. If anything fails:

```bash
# 1. Verify all services
for svc in cloudflared tailscaled mediamtx-webcam robothor-orchestrator \
  robothor-vision robothor-status robothor-status-dashboard robothor-voice \
  robothor-dashboard robothor-privacy robothor-transcript moltbot-gateway \
  robothor-crm robothor-bridge robothor-app; do
  printf "%-35s %s\n" "$svc" "$(sudo systemctl is-active $svc)"
done

# 2. If orchestrator didn't start (depends on ollama + postgres + docker)
sudo systemctl restart robothor-orchestrator

# 3. If Docker containers are down (Vaultwarden, Uptime Kuma, Kokoro TTS)
sudo systemctl restart robothor-crm
```
