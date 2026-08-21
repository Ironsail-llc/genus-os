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

## Why the alert unit has no systemd start limit

`robothor-alert@.service` sets `StartLimitIntervalSec=0` — deliberately, and
against the usual systemd advice. It previously carried
`StartLimitIntervalSec=3600` / `StartLimitBurst=5`, and on 2026-08-20 two
crash-looping services tripped that limit 60 times:

```
robothor-alert@robothor-orchestrator.service.service: Failed with result 'start-limit-hit'.   (31x)
robothor-alert@robothor-bridge.service.service:       Failed with result 'start-limit-hit'.   (29x)
```

A flapping service was silencing its own pager for an hour — exactly the case
the pager exists for. Dedup belongs in the sender, which already suppresses
repeat pages per unit for `ROBOTHOR_ALERT_COOLDOWN_SECONDS` (default 1h) and
only stamps that cooldown after a *delivered* send. Do not restore the start
limit; `tests/test_liveness_watchdog.py` fails if it comes back.

## Liveness watchdog (the path that does not use OnFailure=)

`OnFailure=` is a single, best-effort, in-band hook. It fires exactly once, and
it does not fire at all when:

- the process is **SIGKILLed** during a shutdown/boot transaction — on
  2026-08-19 13:50 systemd logged `robothor-engine.service: Failed to enqueue
  OnFailure= job, ignoring: Transaction for
  robothor-alert@robothor-engine.service.service/start is destructive (...)`
  and no page was sent;
- the process is **wedged but running** — it never enters `failed`, so nothing
  fires.

`robothor-liveness.timer` closes both. Every 5 minutes it runs
`scripts/liveness_probe.sh`, which probes the engine's unauthenticated `/live`
endpoint from outside and pages through the same sender after N *consecutive*
failures (a single blip does not page; any success resets the count). It shares
no code, no process, and no systemd dependency with the engine it watches.

```bash
sudo scripts/install-units.sh          # installs the .service and .timer
sudo systemctl daemon-reload
sudo systemctl enable --now robothor-liveness.timer
systemctl list-timers robothor-liveness.timer
```

| Variable | Default | Meaning |
|---|---|---|
| `ROBOTHOR_LIVENESS_URL` | `http://127.0.0.1:$ROBOTHOR_ENGINE_PORT/live` | endpoint to probe |
| `ROBOTHOR_LIVENESS_FAILURE_THRESHOLD` | `3` | consecutive failures before paging (~15 min at a 5 min interval) |
| `ROBOTHOR_LIVENESS_TIMEOUT` | `10` | per-probe seconds — a wedged engine accepts the connection and never answers |
| `ROBOTHOR_LIVENESS_UNIT` | `robothor-engine.service` | unit named in the page; its journal tail is quoted |
| `ROBOTHOR_LIVENESS_STATE_DIR` | `/run/robothor/liveness` | consecutive-failure counter (tmpfs: resets on reboot) |
| `ROBOTHOR_LIVENESS_PROBE_CMD` | — | replaces the curl probe (tests, non-HTTP probes) |
| `ROBOTHOR_LIVENESS_ALERT_CMD` | `send_failure_alert.sh` | replaces the sender; the unit name is appended |

Set them in `/etc/robothor/robothor.env` (the unit's `EnvironmentFile=`).

An undelivered page is not treated as success: the probe checks the sender's
exit status, logs `page for <unit> was NOT delivered`, fails the unit (which
fires its own `OnFailure=`), and leaves the counter armed so the next tick
retries.

### Probe it — do not trust the silence

```bash
# 1. A real outage must produce a real page (maintenance window, ~12 minutes):
sudo systemctl stop robothor-engine.service
journalctl -u robothor-liveness.service -f      # 1/3, 2/3, then "paging"
sudo systemctl start robothor-engine.service    # page should have arrived

# 2. A crash loop must not silence the pager:
for i in $(seq 8); do sudo systemctl start robothor-alert@probe-test.service; sleep 2; done
journalctl -u 'robothor-alert@*' --since '-5min' | grep start-limit-hit   # must be EMPTY
# exactly one page arrives — the rest are deduped by the sender's cooldown
```

## Engine alert severity routing

Application-level alerts go through `robothor/engine/alerts.py::alert()`:
`critical` pages Telegram immediately; `warning`/`info` are written as
`alert_digest` notification rows surfaced by the morning briefing and
heartbeat instead of paging. A failed page falls back to an `alert_fallback`
notification row so the alert still reaches the next briefing.
