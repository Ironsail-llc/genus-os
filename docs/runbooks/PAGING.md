# Failure Paging (OnFailure → Telegram)

Any wired systemd unit that enters `failed` fires `robothor-alert@<unit>.service`,
which posts the unit name, host, and last journal lines to the operator's
Telegram via `scripts/send_failure_alert.sh`.

## Install on an instance

```bash
# 1. Alert template unit (adjust script path to your workspace):
sudo cp infra/systemd/robothor-alert@.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/robothor-alert@.service   # set ExecStart path

# 2. Wire the critical units:
sudo scripts/install_onfailure_alerts.sh \
    robothor-engine.service robothor-bridge.service robothor-orchestrator.service \
    robothor-nats.service robothor-delphi-engine.service
sudo systemctl daemon-reload
```

## Verify

```bash
sudo systemctl start robothor-alert@manual-test
# → a "🔴 manual-test FAILED" style message should arrive on Telegram
```

Scope deliberately small (~6 core units) to avoid alert fatigue; timers'
oneshot services can be added case-by-case once the baseline is quiet.

## Engine alert severity routing

Application-level alerts go through `robothor/engine/alerts.py::alert()`:
`critical` pages Telegram immediately; `warning`/`info` are written as
`alert_digest` notification rows instead of paging. A failed page falls back to
an `alert_fallback` notification row so the alert is not lost.

### Who reads the digest

`robothor/engine/warmup.py` is the consumer. On the operator-facing agent's
heartbeat (`build_warmth_preamble`) and on its first interactive turn
(`build_interactive_preamble`) it reads unread `alert_digest` /
`alert_fallback` rows and renders them at the top of the preamble:

```
--- UNREAD ALERTS (3) ---
Warning/info alerts that did NOT page. Act on them, then clear each with
ack_notification(notificationId=...).
• 2h ago — [warning] Agent 'x' declares unavailable tools — id=<uuid>
```

Bounds: `MAX_ALERT_ROWS` (8) rows and `MAX_ALERT_SECTION_CHARS` (900) chars.
Overflow is announced, not hidden — the agent reads the rest with `get_inbox`.
An empty inbox renders nothing at all.

**Acknowledgement is verified, not assumed.** The preamble is hard-truncated at
`MAX_WARMTH_CHARS` *after* assembly, so a section that was built is not
necessarily a section that was delivered. `_ack_surfaced_alerts` acknowledges
only rows whose id literally appears in the final preamble text; anything the
truncation ate stays unread and comes back next run. This mirrors
`alerts.py`'s `delivered = bool(sent)`. The agent can also clear a row itself
with the `ack_notification` tool once it has acted on it.

### If the digest looks empty when it should not be

1. Confirm rows exist: `get_inbox(agentId="main", unreadOnly=true)`.
2. Confirm they are unread — a dashboard read (`mark_notification_read`) also
   takes a row out of the digest.
3. Confirm the operator agent's manifest declares at least one `warmup:` key;
   the runner only builds the cron preamble when it does. The interactive
   preamble has no such precondition.
