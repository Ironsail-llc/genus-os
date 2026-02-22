---
name: health-check
description: Check health status of all services and infrastructure.
---

# Health Check

Run a comprehensive health check across all configured services.

## Execution

1. Check CRM/Bridge health:
```
crm_health()
```

2. Check all systemd services (customize this list for your deployment):
```sh
exec sudo systemctl is-active $SERVICE_1 $SERVICE_2 $SERVICE_3
```

3. Check key ports are listening (customize port list):
```sh
exec ss -tlnp | grep -E ':($PORT_LIST)'
```

4. Check PostgreSQL:
```sh
exec pg_isready -h 127.0.0.1
```

5. Check Redis:
```sh
exec redis-cli ping
```

6. Check Ollama (if using local models):
```sh
exec curl -sf http://127.0.0.1:11434/api/tags | head -c 200
```

7. Check disk space:
```sh
exec df -h /
```

## Output Format

```
SERVICE HEALTH REPORT

[checkmark/x] Service Name -- status (port)
...

INFRASTRUCTURE:
[checkmark/x] PostgreSQL -- [status]
[checkmark/x] Redis -- [status]
[checkmark/x] Ollama -- [status] ([N] models loaded)

DISK: [usage]% root
```

## Configuration

Customize the service list and port numbers for your deployment:
- Replace `$SERVICE_1 $SERVICE_2 $SERVICE_3` with your systemd service names
- Replace `$PORT_LIST` with your service ports (e.g., `8600|9099|9100|3004`)

## Rules

- Report ALL services, not just failed ones
- If any service is down, suggest restart command
- If disk > 85%, flag as warning
- If any critical service is down, auto-escalate via /escalate
