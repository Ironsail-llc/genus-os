# Service Level Objectives

The reliability targets this instance holds itself to, where each one is
measured, and what to do when one pages.

Two surfaces, deliberately different:

| Surface | Unit | Cadence | What it does |
|---|---|---|---|
| `scripts/slo_probe.sh` | `robothor-slo.timer` | hourly | **Pages** for the three SLOs that must interrupt someone. |
| `scripts/guardrail_watch.py` `check_slos()` + `check_db_slos()` | `robothor-guardrail-watch.timer` | daily | Prints `=== SLOs (database-free) ===` for S4/S5/S8/pool size, then `=== SLOs (database-backed) ===` for S1/S2/S3/S6/S7, and — only when something breached — leaves exactly one `alert_digest` row covering both. A clean morning writes no row. |

The daily surface does not measure S4, S5 or S8 a second way: it runs
`scripts/slo_probe.sh --report`, which evaluates every database-free SLO,
prints one tab-separated row each and pages nobody. Before that, the daily
report read the last-good markers *only* while the probe took the worse of
(marker, newest file) plus a readdir and a volume probe — so on 2026-08-27 the
daily report said `OK` for two days about a volume the pager was calling a
BREACH. The markers live on NVMe; they stay fresh forever after the disk they
describe falls off the bus.

## Why a dead-man and not just OnFailure=

Every unit in the backup chain pages via `OnFailure=`. On 2026-08-27 the
encrypted USB backup volume dropped off the bus and stayed off for two days,
and those units did page — around 22 Telegram messages whose entire content was
a unit name. None of them answered the only question an operator has: *how old
is the newest restorable copy?*

Then it got quieter. `scripts/backup-volume-check.sh` landed as `ExecCondition=`
on those units, so a wedged volume now makes them **skip**
(`Result=exec-condition`) — deliberately, to end a 96-page-a-day storm. A
skipped unit fires no `OnFailure=` at all. A timer that stops firing fails
nothing either.

Both of those signals are **edge-triggered**: they can only speak when a run
happens. `slo_probe.sh` is **level-triggered** — it reads the *age* of the
newest good backup every hour and keeps paging while that age is out of budget.
Fix the volume and it goes quiet by itself; ignore it and it comes back
tomorrow.

## The objectives

| # | SLO | Measured from | Target | On breach |
|---|---|---|---|---|
| S1 | Run success | `agent_runs`, 7d, terminal statuses only, `benchmark:%` excluded | bad ≤ 5% | digest |
| S2 | Heartbeat delivery | `agent_runs`, 24h, main's `heartbeat:%` runs with `delivered_at`, plus the worst `delivered_at - completed_at` over 7d | ≥ 95% delivered **and** lag < 60s | **page** (12h cooldown) when 0 ran in 24h |
| S3 | Pager delivery | `crm_agent_notifications`, 7d, `alert_fallback` rows **+** `failed to send` lines in `robothor-alert@*`'s journal over 7d | 0 lost pages | digest |
| **S4** | **Backup freshness (dead-man)** | `scripts/backup-state.sh` markers + a readdir of the dump dir + the newest `base-*` directory + `backup-volume-check.sh --ro` | local dump < 26h, offsite < 26h, basebackup < 8d | **page** `slo:backup-freshness` (12h cooldown → re-pages daily until fixed) |
| S5 | Liveness | `systemctl show robothor-liveness.service -p Result` + the timer's `LastTriggerUSec`, read hourly | last fired < 1h, last run `success` | **page** `slo:liveness-stale` (12h cooldown) |
| S6 | LLM availability | `agent_runs`, 24h: `All models failed` share, `ollama_chat/%` share | all-failed < 1%/day, local fallback < 30% | **page** `slo:llm-availability` (6h cooldown) at ≥ 5 all-failed in an hour |
| S6 | Credential pool | `keys_from_env('OPENROUTER_API_KEY')` — the pool every model shares | ≥ 2 keys | digest line `pool size N`, never a page |
| S7 | Workflows | `workflow_runs`, 7d, worst workflow | bad ≤ 10% | digest |
| S8 | Guardrail-watch ran | `systemctl show robothor-guardrail-watch.service -p ExecMainExitTimestamp,ExecMainStatus,Result`, read hourly | **completed** < 26h ago (exit 1 = findings reported, still completed) | **page** `slo:guardrail-watch-stale` (12h cooldown) |

S5 and S8 were both the string `OK` until the hourly probe learned to ask
systemd. S8's evidence was the daily report printing itself — which says
nothing at all on the day the report does not run — and S5 asserted that a
timer *exists* rather than that it *fired*. Both are now measured from
`systemctl show`, hourly, by the same level-triggered probe as S4.

The split is the 2026-08-16 ordering discipline: the database-free half
(and the instance manifest validation after it) must already have run and
reported before any query can hang or raise. Five SQL queries inside the
DB-*free* section defeated exactly that.

Statuses in the daily section are three-valued. **`UNEVALUATED` is not `OK`** —
an SLO whose query did not answer is reported in that word, spelled out, and
does not count as a breach (so a database blip cannot page about backups).

## Responding to a page

### `slo:backup-freshness` — S4

The page names every breached tier and its age. Work down it:

```bash
# 1. Is the volume actually usable? (stat() lies; readdir() does not)
scripts/backup-volume-check.sh --ro /mnt/robothor-backup/robothor/db; echo "exit=$?"
#    exit 1 => the volume is wedged or unmounted. `dmesg | tail -50` and
#    `findmnt --target /mnt/robothor-backup` — an `emergency_ro` in the options
#    means the device dropped off the bus and only a remount (often a reboot,
#    if a dm target is holding a kernel reference) brings it back.

# 2. What do the last-good markers say?
for m in last-local-dump last-offsite-ok last-wal-offsite-ok last-basebackup; do
    printf '%-22s %s\n' "$m" "$(cat /var/lib/robothor/backup-state/$m 2>/dev/null || echo MISSING)"
done

# 3. Run the tier that is stale, by hand, and read the failure:
sudo systemctl start robothor-backup-local.service   # or -offsite, -basebackup
journalctl -u robothor-backup-local.service -n 50 --no-pager
```

A marker reading `unknown (no successful run recorded)` means that job has
**never** succeeded on this box — not that it is merely late. On a fresh
install that is expected until the first successful run stamps it.

Two tiers do not stop at the marker, because the marker and the backup live on
**different disks** — markers on NVMe, backups on the volume — so either can
outlive the other:

- the **local dump** tier takes the worse of (marker, newest `*.sql.gz`);
- the **basebackup** tier falls back to the newest `base-*` **directory** under
  `/mnt/robothor-backup/robothor/basebackup` (`ROBOTHOR_BASEBACKUP_DIR`) when
  the marker is missing, probing the volume first. The report then says
  `marker absent; newest base-* directory is Nh old`, and that is **OK** if N is
  inside the budget. Only the `base-<stamp>/` directory counts — the
  `base-<stamp>.backup_label` file beside it is a WAL position, not a
  restorable copy.

Restoring the box, or losing `/var/lib`, loses the markers and keeps the
backups. Paging *"PITR has no starting point"* while a week-old base backup sits
on the volume is how a dead-man gets muted.

The page repeats every 12 hours while the breach stands. It stops on its own
once a successful run stamps a fresh marker; there is nothing to acknowledge.

### `slo:heartbeat-delivery` — S2

Zero heartbeat runs in 24 hours means the operator-facing agent is not running
at all — no briefing, no digest, no delivery. Check the engine and the
scheduler:

```bash
systemctl status robothor-engine.service
curl -fsS http://127.0.0.1:18800/ready   # 503 => a required agent is missing
```

A hollowed-out fleet (a YAML typo removing an agent) answers `/live` with a
static 200 while `/ready` fails — see `robothor-fleet-guard.service`.

### `slo:llm-availability` — S6

Five or more runs in one hour ending in "All models failed" is almost never a
provider outage: **every model shares one credential pool**, so one capped key
takes the whole fleet down. Check the pool before the provider:

```bash
journalctl -u robothor-engine.service --since '1 hour ago' | grep -i 'all models failed' | tail
```

A 403 is not a 401 on OpenRouter — a capped key answers 403 while the
credential is still valid. Spare keys ride `OPENROUTER_API_KEY_2` and up; the
walk stops at the first gap, so a hole in the numbering hides every key after
it.

The daily report counts that pool and prints `pool size N`. A pool of one is
the 2026-08-27 outage waiting to happen — one capped key stopped every model,
because every model shares the pool, and the spare slot was empty. It breaches
in the digest and never pages: a thin pool is a risk, not an outage.

### `slo:liveness-stale` — S5

The engine watchdog itself is the thing that failed. Either its last run did
not succeed or its 5-minute timer has stopped firing, and a timer that stops
firing fails nothing — no `OnFailure=` anywhere covers this.

```bash
systemctl status robothor-liveness.timer robothor-liveness.service
systemctl list-timers robothor-liveness.timer --all
journalctl -u robothor-liveness.service -n 50 --no-pager
```

### `slo:guardrail-watch-stale` — S8

The daily report has stopped completing, so the drift checks, the instance
manifest validation and the whole daily SLO section it carries are producing
nothing — silently, because a unit that does not run fails nothing.

**S8 measures whether the report RAN, not whether it liked what it found.**
`robothor-guardrail-watch.service` is a `Type=oneshot` that exits **1 by
design** whenever it has findings — a drifted drop-in, an invalid manifest, a
guardrail whose effective mode is not the one its manifest records. That exit
is the unit's own `OnFailure=` pager firing, and the findings have already
reached the operator by the time S8 looks. So a fresh `ExecMainExitTimestamp`
with `ExecMainStatus=1` is **OK** for S8; reading it as a breach meant two
pages for one event, the second one saying the opposite of the truth.

S8 breaches on exactly three states:

| State | Why |
|---|---|
| no `ExecMainExitTimestamp` | no run has ever completed on this box |
| `ExecMainExitTimestamp` older than 26h | the report has stopped running |
| `Result` in `timeout`, `signal`, `core-dump`, `watchdog` | the run stopped mid-way, so no findings page fired either |

An exit status that is neither 0 nor 1 also breaches: the report has no
vocabulary for one, so something killed it after it started.

```bash
systemctl status robothor-guardrail-watch.timer robothor-guardrail-watch.service
journalctl -u robothor-guardrail-watch.service -n 100 --no-pager
sudo systemctl start robothor-guardrail-watch.service   # run it by hand and read the output
```

## The restore drill

`robothor-restore-drill.timer` runs `scripts/restore-drill.sh` monthly: it
fetches the newest dump (offsite first — that is the path a box loss actually
takes), restores it into a scratch database, times it, counts what came back,
and drops the scratch database. The result arrives as an `info` notification in
main's heartbeat.

It is **not** `robothor-backup-verify.timer`, which is `backup-offsite.sh` with
`ROBOTHOR_OFFSITE_VERIFY_ONLY=1` — an rclone byte-comparison. That proves the
bytes match; it proves nothing about whether they reconstitute a database.

The drill fails loudly rather than passing quietly in two cases, both from the
2026-08-24 near-miss where the dump glob matched nothing and the pipeline
"succeeded" in 0.09s against an empty database:

- no dump available anywhere → non-zero abort;
- a restore that produces **zero tables** → non-zero, even though `psql` exited
  0. `psql`'s exit status says only that it read the file.

Procedure and measured baselines: [`RESTORE_DRILL.md`](RESTORE_DRILL.md).

## Changing a budget

Every budget is an environment variable, and both surfaces read it under the
**same name** — the daily report runs the probe, and its marker-only fallback
(`backup_freshness_slos()`, used when the probe is missing) reads the same
variables:

| Variable | Default | SLO |
|---|---|---|
| `ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS` | `26` | S4 local dump |
| `ROBOTHOR_SLO_OFFSITE_MAX_HOURS` | `26` | S4 offsite |
| `ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS` | `192` (8d) | S4 basebackup |
| `ROBOTHOR_BASEBACKUP_DIR` | `/mnt/robothor-backup/robothor/basebackup` | S4 basebackup's marker-free fallback (same spelling as `pg-basebackup.sh`) |
| `ROBOTHOR_SLO_GUARDRAIL_WATCH_MAX_HOURS` | `26` | S8 |
| `ROBOTHOR_SLO_LIVENESS_MAX_HOURS` | `1` | S5 |

Set them in `/etc/robothor/robothor.env`, which both units load with
`EnvironmentFile=`:

```bash
# widen the offsite budget to two days
echo 'ROBOTHOR_SLO_OFFSITE_MAX_HOURS=48' | sudo tee -a /etc/robothor/robothor.env
sudo systemctl start robothor-slo.service   # confirm the new budget in the journal
```

A value that is not an integer falls back to the default **and says so** in the
report. A typo that silently widened a budget to infinity would be a dead-man
that reports every backup as fresh — the failure this whole file exists to
prevent.

## The unit's PATH has no `sbin`

`/etc/robothor/robothor.env` sets a `PATH` with **no `/usr/sbin` and no
`/sbin`**, and every unit loads it with `EnvironmentFile=`. `runuser` lives in
`/usr/sbin`, so under systemd the S2/S6 hop resolved to nothing and the failure
came back as *"the query did not answer (database unreachable?)"* — a page an
operator cannot act on, about an outage that was not happening. The same PATH
had already cost the backup volume guard its `dmsetup`.

Both `scripts/slo_probe.sh` and `scripts/restore-drill.sh` therefore **append**
`/usr/sbin:/usr/bin:/sbin:/bin` to whatever `PATH` they are handed
(`ROBOTHOR_SLO_PATH_FALLBACK` / `ROBOTHOR_RESTORE_DRILL_PATH_FALLBACK`).
Appended, never prepended: the point is to make a directory the unit forgot
*reachable*, not to outrank the PATH the operator configured — this instance's
`rclone` is in `/usr/local/bin`.

Each script then **preflights every external tool it needs** with `command -v`
before measuring anything, and exits non-zero naming the ones that do not
resolve:

```
slo_probe: cannot run: 1 tool(s) do not resolve on PATH=...
slo_probe:   MISSING the database hop (ROBOTHOR_SLO_RUNUSER_CMD) — runuser
slo_probe: nothing was measured and nothing was paged — a missing binary is a
           misconfiguration, not an SLO breach.
```

A missing binary must never become an `UNEVALUATED` row or a breach: the probe
cannot tell *"the database is down"* from *"psql is not installed"*, and only
one of those is something a page can ask someone to fix. It must also never
leave `OK` rows behind for tiers the probe never reached — half a measurement
is worse than none, because it looks like a measurement.

## `ROBOTHOR_SLO_DB_CHECKS=0` — the test-only mute

`scripts/slo_probe.sh` reads this switch, and `0` stops it evaluating **S2**
(heartbeat delivery) and **S6** (LLM availability) at all. It exists for
`tests/test_slo_probe.py`, which drives the probe on this box and must never
query the live database.

**It must never be set in production.** Nothing about a muted SLO looks like a
muted SLO: S2 and S6 simply stop being measured, nothing pages, and the daily
report carries no row for them — which is indistinguishable from two targets
that are permanently fine. That is the inert-control shape this whole file
exists to prevent, so the probe announces the mute on **stderr** on every run:

```
slo_probe: !! ROBOTHOR_SLO_DB_CHECKS=0 — S2 (heartbeat delivery) and S6 (LLM
availability) are NOT being measured. This mute is for tests only and must
NEVER be set in production; see docs/runbooks/SLOS.md.
```

If that line is in `journalctl -u robothor-slo.service`, remove the variable
from `/etc/robothor/robothor.env` and restart the unit. To silence a *noisy*
SLO, widen its budget instead — a budget is still a measurement.

The cooldowns are variables too, in seconds:
`ROBOTHOR_SLO_BACKUP_COOLDOWN_SECONDS` (12h),
`ROBOTHOR_SLO_HEARTBEAT_COOLDOWN_SECONDS` (12h),
`ROBOTHOR_SLO_LLM_COOLDOWN_SECONDS` (6h),
`ROBOTHOR_SLO_GUARDRAIL_COOLDOWN_SECONDS` (12h) and
`ROBOTHOR_SLO_LIVENESS_COOLDOWN_SECONDS` (12h).

Related: [`PAGING.md`](PAGING.md) for how a page is delivered and deduped,
[`OFFSITE_BACKUP.md`](OFFSITE_BACKUP.md) and [`PITR.md`](PITR.md) for the
backup tiers themselves.
