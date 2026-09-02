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

## The spool: a page DNS ate is late, not lost

Since 2026-08-31 the journal carries 63 `curl_rc=6` lines — `Could not resolve
host: api.telegram.org`. The `OnFailure=` path survives those, because
`robothor-alert@.service` has `Restart=on-failure` behind it. The callers with
no retrying unit behind them do not: `scripts/cron-wrapper.sh`,
`backup-offsite.sh`, `thermal-guard.sh` and `boot-guard.sh` exhaust the retry
loop, exit 1, and the page is gone.

A longer backoff only helps the path that already retries; a pinned IP breaks on
rotation; `curl --dns-servers` needs a c-ares build. So an exhausted send writes
the page it composed to a **durable spool** and still exits 1:

```
/var/lib/robothor/alert-spool/<epoch>-<key>.msg
```

`/var/lib`, not `/run` — the boot window that breaks a page usually ends in a
reboot, and a tmpfs spool would lose it there.

Draining is not a separate service. Every invocation of the sender drains
first, and `robothor-liveness.timer` (root, every 5 min, `After=network-online`)
runs `send_failure_alert.sh --drain` at the top of each tick, so a healthy
engine still clears the backlog. The drain:

- goes **oldest first** and prefixes each page `⏳ DELAYED (queued HH:MM):`, so
  an hour-old page cannot be misread as a live incident;
- **ignores the cooldown** — a spooled page is one the operator has never seen,
  and the stamp exists to dedup repeats of a page they *have*;
- deletes a file only on a **2xx**, and **stops at the first failure** (the
  endpoint is still down; the rest of the spool would be burned for nothing);
- keeps at most 50 pages, dropping the oldest and saying `N older pages
  dropped` in the log *and* in a Telegram notice — a silent truncation would be
  the pager lying about what it held;
- refuses any spooled page naming a pytest temp path, the same guard the entry
  point applies to the unit name (the spool dir is world-writable, see below).

| Variable | Default | Meaning |
|---|---|---|
| `ROBOTHOR_ALERT_SPOOL_DIR` | `/var/lib/robothor/alert-spool` | durable spool for undelivered pages |
| `ROBOTHOR_ALERT_SPOOL_CAP` | `50` | pages kept; older ones are dropped, loudly |

The directory is `1777` (sticky), created by
`infra/tmpfiles/robothor-restart.conf`: root's units and the operator's cron
jobs both spool into it and neither can chown the other's files, and sticky
keeps each writer able to delete only its own. The cost is that any local user
can plant a `.msg` there, which is why the drain re-applies the fixture-path
refusal to the message body.

```bash
ls -l /var/lib/robothor/alert-spool           # anything here is a page still owed
sudo scripts/send_failure_alert.sh --drain    # deliver it now
```

**Tests must pin `ROBOTHOR_ALERT_SPOOL_DIR`.** A cooldown stamp written by a
test only suppresses a page; a spooled file is a page the next tick will
actually *deliver*. The base envs in `tests/test_pager_hardening.py`,
`tests/test_failure_alerts.py` and `tests/test_liveness_watchdog.py` pin it, and
`test_run_send_default_env_never_spools_to_the_real_dir` fails if that stops
being true.

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
`alert_digest` notification rows instead of paging. A failed page falls back to
an `alert_fallback` notification row so the alert is not lost.

### `ROBOTHOR_ALERT_SELFTEST` must not page critical in production

`ROBOTHOR_ALERT_SELFTEST=1` makes the engine fire one alert shortly after
startup (`robothor/engine/daemon.py::_maybe_run_alert_selftest`). It fires at
`info`, so it lands as an `alert_digest` row and the operator agent surfaces it
on the next heartbeat. **Leave it at `info`.**

It has been wrong in both directions. At first the probe fired at `info` while
claiming to verify Telegram delivery end-to-end — which `info` cannot do, so it
was a probe that could not fail. Raising it to `critical` made it honest and
made it a pager: the engine restarts, so the flag paged CRITICAL on *every
start* — 52 pages in 7 days, none of them an incident. A self-test that trains
the operator to scroll past red costs more than the blind spot it closed.

What the probe proves is that `alert()` runs and reaches durable storage; the
row write is checked, not assumed, and a failure is logged at ERROR. It does
not prove Telegram delivery, and it is not supposed to. Those paths prove
themselves: `send_failure_alert.sh` verifies its send by HTTP status (a 401 is
not a delivery) and spools what it could not send, and the liveness watchdog
checks the sender's exit code. To confirm a page really lands, use the Verify
step at the top of this runbook — a deliberate, one-off `robothor-alert@manual-test`.

`robothor/engine/tests/test_alert_selftest.py::test_the_selftest_never_pages`
fails if the level goes back up.

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
