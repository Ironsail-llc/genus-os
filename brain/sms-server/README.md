# Robothor SMS Webhook

HTTP endpoint that receives incoming SMS messages from Twilio, logs them, and responds.

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check (redirects to /health) |
| `/health` | GET | Service status and message count |
| `/sms` | POST | Twilio webhook for incoming SMS |
| `/messages` | GET | List recent messages (JSON) |

## Twilio Configuration

**Phone Number:** +1 (413) 408-6025
**Webhook URL:** `https://sms.robothor.ai/sms`
**Method:** POST

To update the webhook URL:
```bash
curl -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_SID}/IncomingPhoneNumbers/${PHONE_SID}.json" \
  --user "${TWILIO_SID}:${TWILIO_AUTH}" \
  --data-urlencode "SmsUrl=https://sms.robothor.ai/sms" \
  --data-urlencode "SmsMethod=POST"
```

## Usage

### Send SMS (outbound)
```bash
TWILIO_SID="AC65d10c9ae90e8374fb242e06d41c6aa0"
TWILIO_AUTH="235983cd323dadcfa3832ce56d3649e3"

curl -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_SID}/Messages.json" \
  --user "${TWILIO_SID}:${TWILIO_AUTH}" \
  --data-urlencode "To=+13479061511" \
  --data-urlencode "From=+14134086025" \
  --data-urlencode "Body=Hello from Robothor!"
```

### Check received messages
```bash
curl -s http://localhost:8766/messages | python3 -m json.tool
```

### Health check
```bash
curl -s http://localhost:8766/health
```

## Log Format

Messages are stored in `/home/philip/clawd/memory/sms-log.json`:
```json
{
  "receivedAt": "2026-02-12T11:10:53.100741",
  "messageSid": "SMxxxxx",
  "from": "+1234567890",
  "to": "+14134086025",
  "body": "Message text",
  "numMedia": 0,
  "mediaUrls": [],
  "repliedAt": null,
  "replyBody": null
}
```

## Running the Server

The server runs automatically via systemd. To manage it:

```bash
# Check status
systemctl --user status robothor-sms

# Restart
systemctl --user restart robothor-sms

# View logs
journalctl --user -u robothor-sms -f
```

Or run manually:
```bash
cd /home/philip/clawd/sms-server
python3 server.py --port 8766
```

## Cloudflare Tunnel

The webhook is exposed via Cloudflare Tunnel at `sms.robothor.ai`.

Config: `/home/philip/.cloudflared/config.yml`
```yaml
- hostname: sms.robothor.ai
  service: http://localhost:8766
```

If the tunnel is using managed config (token-based), update the ingress rules via the Cloudflare Zero Trust dashboard.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8766 | Server port |

## Credentials

Stored in environment (not in repo):
- `TWILIO_SID`: Account SID
- `TWILIO_AUTH`: Auth Token
- `TWILIO_PHONE`: +14134086025

See `/home/philip/moltbot/docs/TOOLS.md` Twilio section for full details.
