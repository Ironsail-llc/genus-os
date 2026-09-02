# Failure Paging (OnFailure → Telegram)

Any wired systemd unit that enters `failed` fires `robothor-alert@<unit>.service`,
which posts the unit name, host, and last journal lines to the operator's
Telegram via `scripts/send_failure_alert.sh`.

## Install on an instance

```bash
# 1. Render and install every robothor-* unit, robothor-alert@.service included.
#    Do NOT hand-copy it: scripts/install-units.sh runs each template through
#    scripts/render-unit.sh (which refuses an unexpanded placeholder or a %h),
#    gates the .service files on `systemd-analyze verify`, and installs only if
#    every one of them rendered. A hand-edited copy in /etc is the drift the
#    installer exists to end — it is why template fixes in the repo never
#    reached the box.
sudo scripts/install-units.sh
sudo systemctl daemon-reload

# 2. Wire anything the repo does not already carry a drop-in for.
#    install-units.sh installs robothor-*.service.d/*.conf too, so
#    robothor-engine, -bridge, -orchestrator and -vision are already wired by
#    step 1 (each has a mirrored onfailure.conf). Name only the rest:
sudo scripts/install_onfailure_alerts.sh robothor-nats.service
sudo systemctl daemon-reload
```

`install-units.sh` needs `ROBOTHOR_WORKSPACE` and `ROBOTHOR_SERVICE_USER`; it
resolves anything unset from `/etc/robothor/robothor.env` (override with
`--env-file`). It is idempotent — a second run reports `unchanged` — and it
does not reload or restart anything, so the `daemon-reload` above is yours to
run.

## Verify

```bash
sudo systemctl start robothor-alert@manual-test
# → a "🔴 manual-test FAILED" style message should arrive on Telegram
```

Scope deliberately small (a handful of core units) to avoid alert fatigue;
timers' oneshot services can be added case-by-case once the baseline is quiet.
Add any instance-land units of your own to the same command — the installer
only ships `robothor-*` templates and refuses to touch anything else, but
`install_onfailure_alerts.sh` writes a `<unit>.d/onfailure.conf` for whatever
unit names you give it. Prefer mirroring the drop-in into
`infra/systemd/<unit>.service.d/` for a platform unit, so `install-units.sh`
keeps it and `check_dropin_drift.sh` can see it drift.

## What a page means

Line 2 of every composed page is the **consequence**: what the operator has
actually lost, not what systemd noticed. It exists because
`🔴 <unit> FAILED on <host>` is a fact about systemd and nothing else — ~50 of
those were scrolled past while every backup path was down, because the text
gave no way to tell "a log shipper is a few minutes behind" from "there is no
restorable copy of the database tonight". It sits on line 2 so it lands inside
Telegram's notification preview, legible without opening the message.

`consequence_for()` in `scripts/send_failure_alert.sh` is the whole map:

| Key matches | Consequence line | First thing to check |
|---|---|---|
| `*wal-offsite*` | PITR recovery point aging past 15 min — WAL has stopped shipping; last good ship: *(marker)* | `docs/runbooks/PITR.md`; `journalctl -u robothor-wal-offsite` |
| `*backup-local*` | Nightly dump did NOT happen; newest good: *(marker)*; +24h dump-tier RPO/night | the backup volume — `docs/runbooks/BACKUP_VOLUME_GUARD.md` |
| `*backup-offsite*`, `*offsite-backup*` | Offsite NOT refreshed; a box loss restores from *(marker)* | `docs/runbooks/OFFSITE_BACKUP.md`; rclone remote reachable? |
| `*basebackup*` | No fresh base backup; PITR must replay every WAL since *(marker)* — restore time growing nightly | `docs/runbooks/PITR.md` |
| `*backup-verify*` | Backups are UNVERIFIED — a corrupt archive would now go unnoticed until a restore is attempted | `docs/runbooks/OFFSITE_BACKUP.md` |
| `robothor-engine.service` | Agents are DOWN — no scheduled runs, no heartbeat, no delivery until this is back | the engine journal; the liveness watchdog below |
| `*bridge*` | Inbound/outbound channel bridge is down — messages to and from the operator are not moving | the bridge journal |
| `*orchestrator*` | Workflows are not being scheduled or advanced; approvals and queued work sit untouched | the orchestrator journal |
| `*nats*` | The message fabric is down — agent mail and federation traffic are dropping, not queuing | `docs/runbooks/FEDERATION.md` |
| `robothor-vision.service`, `robothor-vision*` | Vision capture is down — no camera events; presence and face recognition are blind | the vision journal |
| `*liveness*` | The liveness watchdog itself is down — nothing is checking whether the engine is alive | this runbook, "Liveness watchdog" below |
| anything else | `(no consequence mapped — add one in send_failure_alert.sh)` | add a case; an unmapped page is a page nobody can triage from the preview |

Three things about that table are load-bearing:

- **Matched as substrings, first match wins.** The same condition arrives under
  three spellings — a unit (`robothor-wal-offsite.service`), a cron pseudo-unit
  (`cron: ... wal-offsite.sh (exit 1)`), and a script's own label
  (`offsite-backup: ...`) — so the specific patterns are listed before the
  general ones and a new case must go in the right place.
- **`engine` and `vision` are anchored, not bare substrings.** `*engine*`
  matched `search-engine` and `*vision*` matched `provision` and `supervision`,
  so both are pinned to the real unit name instead.
- **The marker is read from `/var/lib/robothor/backup-state` on NVMe**, never
  from the volume that just failed
  (`ROBOTHOR_BACKUP_STATE_DIR`; `scripts/backup-state.sh`). An absent marker
  renders as `unknown (no successful run recorded)` — deliberately, because an
  empty string where a timestamp belongs reads as "recent" and means the
  opposite.

The consequence line is only added to the **composed** page. A two-argument
call supplies its own body and gets neither a headline nor a consequence — see
below.

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

## Two ways to call the sender

```bash
send_failure_alert.sh <unit>            # the sender composes the page
send_failure_alert.sh <key> "<body>"    # the caller composes the page
```

**One argument** is the `OnFailure=` shape: `<unit>` is both the dedup key and
the headline, and the page is built from it — `🔴 <unit> FAILED on <host>`, the
consequence line for that unit, the timestamp, and the tail of that unit's
journal.

**Two arguments** and the *body is the page*. Nothing is prepended to it; the
only thing added is a `<timestamp> on <host>` trailer. `<key>` is then a dedup
key and nothing else — it never appears in the message. That matters because
the composed shape actively contradicts some callers: a RECOVERY notice sent
with one argument paged as `🔴 backup-volume-recovered FAILED` above its own ✅
line, and a key outside the consequence map appended `(no consequence mapped —
add one in send_failure_alert.sh)`, a note for whoever maintains this script,
to the operator's phone.

The cooldown still keys on `<key>`, so pick a stable one per condition: pages
for the same key inside `ROBOTHOR_ALERT_COOLDOWN_SECONDS` (1h) are suppressed,
and a key that varies per run defeats that.

### The cooldown falls back when the caller is not root

The stamp files live in `ROBOTHOR_ALERT_STATE_DIR`
(`/run/robothor/alert-cooldown`), which the systemd units create `root:root`
`0755`. Cron runs as the operator's own user, so for every cron-driven page the
`touch` failed silently — it is `|| true`, because a broken stamp must never
block a real page — and the next run re-read an empty state dir and paged
again. A crontab entry pointing at a deleted script paged once a day for 129
days, and the backup storm paged with no dedup at all.

So when `ROBOTHOR_ALERT_STATE_DIR` is not writable, **both** the read and the
stamp move to a per-uid directory — moving only the stamp would leave dedup
half-wired, reading a directory nothing ever writes:

| Order | Fallback |
|---|---|
| 1 | `ROBOTHOR_ALERT_FALLBACK_STATE_DIR`, if set |
| 2 | `$XDG_RUNTIME_DIR/robothor-alert-cooldown` when that dir exists and is writable — tmpfs, per-user, and gone on logout/reboot like `/run/robothor` itself |
| 3 | `/tmp/robothor-alert-cooldown-$(id -u)` |

It is created `0700`, and then **re-checked**: `mkdir -p` succeeds silently on
an existing directory, does not chmod one, and follows a symlink, so a local
user who pre-creates that exact path could plant stamps and suppress a real
page. If the leaf is not a real directory owned by this user, **dedup is
disabled for that send** rather than the send being aborted — a page must never
be suppressed by an untrusted directory, and never dropped either. Both
outcomes are named on stderr (`using cooldown state dir ...`, or
`... dedup disabled for this send`), because a cooldown that moves silently is
one nobody can find when they go looking for why a page did not arrive.

Root's path is untouched: root can write `/run/robothor/alert-cooldown`, so
none of this fires.

**A test that can reach the pager must pin the cooldown dirs as well as the
spool** — see the end of the spool section below.

The form exists for the backup volume guard, whose `backup-volume-degraded` /
`backup-volume-recovered` notices are the case that broke the composed shape:
a ✅ recovery paged under a `🔴 ... FAILED` headline, with a maintenance note
where the consequence line belongs.

`scripts/thermal-guard.sh` and `scripts/boot-guard.sh` still use the single
argument, passing their whole message as the key
(`"THERMAL-CRITICAL 96C — clean reboot now"`). That works — it is also the
shape this form replaces, since the sender then wraps that message in a
headline, an unmapped consequence line, and the journal tail of a unit that
does not exist. Moving them over is a `<stable key> "<message>"` change, and
the stable key is what gives them an hour of dedup.

Whatever composes the body, the fixture-path guard reads it as well as the
key, so a body built from a path cannot page a pytest tmpdir.

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
- deletes a file only on a **2xx** — and the delete is **checked**: in a 1777
  spool this account may not own the file, and `rm` failing silently meant the
  same page went out again on every tick while the log called it delivered. A
  non-root drain takes only files it owns and leaves the rest to root's tick;
- **stops at a 5xx or a network failure** (the endpoint is down; the rest of
  the spool would be burned for nothing) and counts the attempt in a
  `<file>.attempts` sidecar;
- **quarantines** a page it cannot ever deliver into
  `/var/lib/robothor/alert-spool/poison/` and carries on with the next one: a
  content rejection (a 4xx other than 401/403/408/429 — those mean bad
  credentials or rate limiting, which apply to every page equally), or a file
  past `ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS`, or one older than
  `ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS`. Before this, one rejected page sat
  at the head of the queue and nothing behind it was ever delivered;
- keeps at most 50 pages, dropping the oldest and saying `N older pages
  dropped` in the log. The **Telegram notice is deferred**: a drop only ever
  happens mid-outage, which is the one moment the notice cannot be sent, so
  the count is appended to `<spool>/.dropped` and the next page that actually
  lands carries it as its first line; the counter is cleared only after that
  page is delivered. Several drops in one outage add up to a single notice;
- logs (and keeps) an empty or unreadable `.msg` rather than deleting it — it
  leaves the queue when it ages out into `poison/`;
- refuses any spooled page naming a pytest temp path, the same guard the entry
  point applies to the unit name (the spool dir is world-writable, see below).

| Variable | Default | Meaning |
|---|---|---|
| `ROBOTHOR_ALERT_SPOOL_DIR` | `/var/lib/robothor/alert-spool` | durable spool for undelivered pages |
| `ROBOTHOR_ALERT_SPOOL_CAP` | `50` | pages kept; older ones are dropped, loudly |
| `ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS` | `48` | failed deliveries before a page is quarantined (≈4h of 5-min ticks) |
| `ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS` | `86400` | age at which a page is quarantined unsent |
| `ROBOTHOR_ALERT_JOURNAL_CMD` | `journalctl` | journal reader for the page's tail (a test seam) |

Two dotfiles live alongside the pages: `.dropped` (truncation notices owed
to the operator) and `.stuck` (one line saying why the queue is not moving —
see below). Neither is a page and neither is drained.

Anything in `poison/` is a page that was **never delivered**. Read it, then
delete it — nothing else will:

```bash
ls -l /var/lib/robothor/alert-spool/poison/   # pages given up on, with the reason in the journal
journalctl -u robothor-liveness.service | grep QUARANTINED   # why each one was given up on
```

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

### When you will hear that the spool is stuck

`--drain` exits 0 whatever happens, and it has to: a backlog is not an
incident, and failing the liveness unit over one would fire that unit's own
`OnFailure=` page about the outage that filled the spool. The consequence used
to be that a revoked token, or a queue nothing had moved in a day, produced
journal lines and nothing else — the spool promises a page is *late*, not
lost, and a queue that cannot move breaks that promise in silence.

So the queue reports on itself, in two steps:

1. A drain that gives up mid-queue writes one line to
   `/var/lib/robothor/alert-spool/.stuck` — the timestamp, the head page, and
   the `curl_rc` / HTTP status it stopped on. It is written only when the head
   page has burned **half** the attempt budget (24 of 48 ≈ 2h of ticks) or has
   been queued past **half** the age cap (12h). Below that this is a DNS blip
   and the next tick clears it. Any **delivered** page removes the marker, as
   does a drain that finds the queue empty.
2. `scripts/liveness_probe.sh` reads that marker on every 5-minute tick. Once
   it has stood for 30 minutes (`ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS`) it
   counts as a probe failure on its own key, `alert-spool-stuck` — its own
   counter, so a healthy engine cannot reset it — and after the usual
   `ROBOTHOR_LIVENESS_FAILURE_THRESHOLD` consecutive ticks it pages:

   ```
   🔴 alert spool STUCK 47m: 2026-09-02T14:40:12+01:00 1756... undelivered
      after 24/48 attempts (curl_rc=0 http_status=401)
   ```

   Deliberately one short line: the delivery path is the thing that is broken,
   so the smallest page is the one most likely to get through. If even that
   send fails, the probe exits non-zero and `robothor-liveness.service`'s own
   `OnFailure=` is what carries it — that hook, not this page, is the real
   floor.

Worst case the operator hears about a stuck spool ~35 minutes after it goes
stuck, and a stuck spool is by definition at least 2h (attempts) or 12h (age)
into the outage that caused it.

When that page arrives:

```bash
cat  /var/lib/robothor/alert-spool/.stuck      # what the drain stopped on
ls -l /var/lib/robothor/alert-spool/*.attempts # how hard it has tried, per page
ls -l /var/lib/robothor/alert-spool/poison/    # pages already given up on
```

Read `curl_rc` **first**. It is the field that says whether the message or the
network is the problem, and on this platform the common answer is the network:
one 2026-08-31 window put 63 `curl_rc=6` lines in the journal and not one of
them was a page anybody needed to act on.

- `curl_rc=6` — DNS (`Could not resolve host: api.telegram.org`). **The
  overwhelmingly common case, and not an alerting fault at all**: the spool is
  doing exactly its job, the pages are late rather than lost, and there is
  nothing to fix but the network. Do not rotate a token over this.
- `curl_rc=0` with an HTTP status — curl reached Telegram and Telegram
  answered. Now the status matters:
- `http_status=401` or `403` — the bot token is wrong or revoked. Check
  `ROBOTHOR_TELEGRAM_BOT_TOKEN` in `/run/robothor/secrets.env`, rotate it with
  BotFather, re-encrypt, `scripts/decrypt-secrets.sh`, then
  `sudo scripts/send_failure_alert.sh --drain`. The marker clears itself on
  the first delivered page.
- `http_status=400` on page after page — the pages themselves are being
  refused; they are in `poison/`, and reading one shows what Telegram would
  not take. The usual cause is a body that is not valid UTF-8: the journal
  tail is sliced by **bytes** (`tail -c`, `ROBOTHOR_ALERT_JOURNAL_TAIL_BYTES`,
  default 500) and journal lines carry arbitrary bytes of their own. The
  sender now takes a little more than the budget, drops what does not
  round-trip through `iconv -c -f utf-8 -t utf-8`, cuts to the real budget,
  then scrubs the new boundary too — the second cut can split a character just
  as the first one could. Without `iconv` (a from-scratch container) it falls
  back to stripping to ASCII: losing the em dashes beats losing the page. A
  400 here means something got past that, so read the file.
- A marker with an EMPTY spool means nothing could clear it — almost always a
  `.stuck` owned by the other account in the 1777 dir. The drain says so in
  the journal (`could not clear ... (not owner)`); remove it as that user.

**Tests must pin `ROBOTHOR_ALERT_SPOOL_DIR`.** A cooldown stamp written by a
test only suppresses a page; a spooled file is a page the next tick will
actually *deliver*. The base envs in `tests/test_pager_hardening.py`,
`tests/test_failure_alerts.py` and `tests/test_liveness_watchdog.py` pin it, and
`test_run_send_default_env_never_spools_to_the_real_dir` fails if that stops
being true. Repo-wide,
`tests/test_alert_never_pages_from_tests.py::test_every_test_that_can_page_pins_the_spool_and_the_state_dirs`
fails, naming the file, if any test that runs the pager leaves the spool or
either cooldown dir unpinned — redirecting the API is not enough once an
undelivered page is spooled rather than dropped.

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
| `ROBOTHOR_ALERT_SPOOL_DIR` | `/var/lib/robothor/alert-spool` | the spool this tick drains, and where it reads `.stuck` |
| `ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS` | `1800` | how long a `.stuck` marker may stand before it is a probe failure |

Set them in `/etc/robothor/robothor.env` (the unit's `EnvironmentFile=`).

An undelivered page is not treated as success: the probe checks the sender's
exit status, logs `page for <unit> was NOT delivered`, fails the unit (which
fires its own `OnFailure=`), and leaves the counter armed so the next tick
retries.

The tick answers two questions, not one: *is the engine answering* (keyed on
`ROBOTHOR_LIVENESS_UNIT`) and *is the alert spool moving* (keyed on
`alert-spool-stuck`). Separate counters, because sharing one would let a
recovering engine reset the spool's count every tick. See
[When you will hear that the spool is stuck](#when-you-will-hear-that-the-spool-is-stuck).

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
