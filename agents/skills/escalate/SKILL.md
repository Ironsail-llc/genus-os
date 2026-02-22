---
name: escalate
description: Escalate an issue with structured context via the configured alert channel.
---

# Escalate

Create a structured escalation and deliver it to the owner via the configured alert channel.

## Inputs

- **issue**: Description of the issue (required)
- **urgency**: high, medium, or low (default: medium)
- **source**: Where this came from -- email, calendar, crm, vision, system (required)

## Execution

1. Read current escalations:
```sh
exec cat memory/worker-handoff.json
```

2. Add new escalation entry with:
   - `id`: UUID
   - `source`: the source system
   - `urgency`: high/medium/low
   - `summary`: concise issue description
   - `details`: full context
   - `createdAt`: ISO timestamp
   - `surfacedAt`: null (supervisor will set this)
   - `resolvedAt`: null

3. Write updated worker-handoff.json

4. If urgency is "high", immediately notify via the alert channel:
```
message(channel="$DELIVERY_CHANNEL", to="$ALERT_CHANNEL", text="<FORMATTED_ALERT>")
```

## Alert Format

```
[urgency emoji] ESCALATION ([source])
[summary]

Context: [details]
Action needed: [suggested action]
```

## Configuration

Replace `$DELIVERY_CHANNEL` with your messaging channel (e.g., `telegram`, `discord`, `slack`).
Replace `$ALERT_CHANNEL` with the target chat/channel ID for high-urgency alerts.

## Rules

- High urgency: notify immediately via the alert channel
- Medium/low urgency: write to handoff file, supervisor picks up on next heartbeat
- Never create duplicate escalations for the same issue
- Check worker-handoff.json for existing similar entries first
