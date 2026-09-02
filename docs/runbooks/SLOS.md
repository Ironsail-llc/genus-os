# Service Level Objectives

The reliability targets this instance holds itself to, where each one is
measured, and what to do when one pages.

Two surfaces, deliberately different:

| Surface | Unit | Cadence | What it does |
|---|---|---|---|
| `scripts/slo_probe.sh` | `robothor-slo.timer` | hourly | **Pages** for the three SLOs that must interrupt someone. |
| `scripts/guardrail_watch.py` `check_slos()` | `robothor-guardrail-watch.timer` | daily | Prints the `=== SLOs ===` section for **all** of them and leaves one `alert_digest` row for the heartbeat. |

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
| S2 | Heartbeat delivery | `agent_runs`, 24h, main's `heartbeat:%` runs with `delivered_at` | ≥ 95% delivered | **page** (12h cooldown) when 0 ran in 24h |
| S3 | Pager delivery | `crm_agent_notifications`, 7d, `alert_fallback` rows | 0 lost pages | digest |
| **S4** | **Backup freshness (dead-man)** | `scripts/backup-state.sh` markers + a readdir of the dump dir + `backup-volume-check.sh --ro` | local dump < 26h, offsite < 26h, basebackup < 8d | **page** `slo:backup-freshness` (12h cooldown → re-pages daily until fixed) |
| S5 | Liveness | `robothor-liveness.timer` | 100% | already wired — pages through its own probe |
| S6 | LLM availability | `agent_runs`, 24h: `All models failed` share, `ollama_chat/%` share | all-failed < 1%/day, local fallback < 30% | **page** `slo:llm-availability` (6h cooldown) at ≥ 5 all-failed in an hour |
| S7 | Workflows | `workflow_runs`, 7d, worst workflow | bad ≤ 10% | digest |
| S8 | Guardrail-watch ran | the daily report itself | daily | page via the unit's `OnFailure=` |

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

Budgets are environment variables with defaults in `scripts/slo_probe.sh` and
`scripts/guardrail_watch.py` (`BACKUP_SLO_BUDGET_HOURS`). Set them in
`/etc/robothor/robothor.env` so both surfaces agree; a budget changed in one
place is a dead-man measuring something the daily report does not.

Related: [`PAGING.md`](PAGING.md) for how a page is delivered and deduped,
[`OFFSITE_BACKUP.md`](OFFSITE_BACKUP.md) and [`PITR.md`](PITR.md) for the
backup tiers themselves.
