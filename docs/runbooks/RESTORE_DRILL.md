# Database Restore Drill

Backups that have never been restored are hope, not backups. This runbook is
the drill procedure plus the measured baseline; re-run it after any change to
the backup pipeline and update the table below.

## Automated

`robothor-restore-drill.timer` runs `scripts/restore-drill.sh` **monthly** — the
procedure below, unattended, with the result written as an `info` notification
into main's heartbeat. It was quarterly-by-hand and got run twice in five
months, which is how long a drill stays a plan.

The script carries the two guards this runbook learned the hard way: an empty
dump aborts non-zero, and a restore that produces **zero tables** fails even
though `psql` exited 0. Run it by hand with
`sudo systemctl start robothor-restore-drill.service`, or read
[`SLOS.md`](SLOS.md) for how it fits alongside the backup-freshness dead-man.

Before any of that, the script refuses to run at all against an unsafe scratch
name: `ROBOTHOR_RESTORE_DRILL_DB` (default `robothor_restore_drill`) must
contain the substring `drill`, and it must not equal the live database
(`ROBOTHOR_DB_NAME`), `postgres`, `template0` or `template1`. The only
destructive verb in the script is `dropdb`, so this check runs before a
connection is opened and before the `EXIT` trap that calls `dropdb` is even
installed.

Before it creates anything, the script resolves every tool it needs
(`psql`, `createdb`, `dropdb`, `timeout`, `rclone` when a remote is set) with
`command -v` and aborts naming the ones that are missing. It resolves them on a
PATH it builds itself — the unit loads `/etc/robothor/robothor.env`, whose
`PATH` has no `/usr/sbin` and no `/sbin` and *does* begin with a user-writable
`~/.local/bin`, which is not a PATH a root-run drill should inherit. A missing
binary reported as a restore failure is the one wrong conclusion this drill
must never reach — see [`SLOS.md`](SLOS.md), "Both scripts build their own
PATH", for the exact line and for `ROBOTHOR_EXTRA_PATH`, the test-only seam.

### `dropdb` is bounded

`dropdb` blocks for as long as **any** backend is still connected to the
target, and it waits forever. Both of the drill's drops — the one before the
restore and the one in the EXIT trap — therefore run under
`timeout ${ROBOTHOR_RESTORE_DRILL_DROP_TIMEOUT:-300}`. A blown budget names
`dropdb`, exits non-zero (so the unit's `OnFailure=` pages) and **leaves the
scratch database in place**; without the cap the drill sat there until
`TimeoutStartSec=7200`, which reads as a drill that never finished rather
than a cleanup that could not complete.

The timeout latches: once a drop has been shown to hang, the EXIT trap does
not retry it and buy a second full budget on the way out. To clear the
leftover, find what is holding it and drop it by hand:

```sh
psql -d postgres -c "SELECT pid, state, query FROM pg_stat_activity WHERE datname = 'robothor_restore_drill'"
dropdb robothor_restore_drill
```

The most common holder is a second drill (or a test run) overlapping this
one. The live-DB tests carry `@pytest.mark.integration` for that reason —
run them deliberately, not as part of a default `pytest`.

It is **not** `robothor-backup-verify.timer`. That is `backup-offsite.sh` with
`ROBOTHOR_OFFSITE_VERIFY_ONLY=1`: an rclone byte-comparison, which proves the
bytes match and nothing about whether they reconstitute a database.

## Procedure (by hand)

```bash
# 0. ASK THE MARKER, NOT THE GLOB. The glob answers "is there a file", which is
#    a question about the mount. The marker answers "did a backup actually
#    succeed, and when" — it is written on the LAST line of a successful run by
#    scripts/backup-state.sh, and it lives on NVMe, never on the volume that
#    breaks. A wedged volume leaves the glob matching nothing (2026-08-24) or,
#    worse, matching a stale generation that looks current.
#
#    Absent or empty reads as "unknown (no successful run recorded)", NOT as an
#    empty string — an empty value where a timestamp belongs is scanned as
#    "recent" and means the opposite.
cat "${ROBOTHOR_BACKUP_STATE_DIR:-/var/lib/robothor/backup-state}/last-local-dump"
# 2026-09-02T04:30:11+02:00 robothor_memory-20260902.sql.gz
#
# Field 1 is the timestamp (with its UTC offset, so it stays orderable against
# `now`); field 2 is the identifier — for this marker, the dump filename. If it
# is hours older than you expect, the drill you are about to run is a drill on
# an old generation and the finding is already in front of you. Compare with
# last-offsite-ok before deciding which copy to drill from.

# 1. The dump the marker names (produced by scripts/backup-ssd.sh at 04:30).
#    GUARD THE GLOB anyway: on 2026-08-24 the backup SSD had USB-disconnected
#    and the glob matched NOTHING — and the drill pipeline below then
#    "succeeded" in 0.09s against an empty database. An empty dump variable
#    must abort.
MOUNT="${ROBOTHOR_BACKUP_MOUNT:-/mnt/robothor-backup}"
DUMP=$(ls -t "$MOUNT"/robothor/db/robothor_memory-*.sql.gz 2>/dev/null | head -1)
[ -n "$DUMP" ] || { echo "NO LOCAL DUMP — mount gone? Drill from offsite instead:"; \
                    echo "  rclone copy <remote>/db/<newest>.sql.gz /tmp/"; exit 1; }

# 1b. IS IT A WHOLE FILE? gunzip -t reads and decompresses the entire archive
#     and checks its CRC without writing anything. A dump truncated by a volume
#     that dropped mid-write decompresses cleanly for hundreds of megabytes and
#     then stops — and `gunzip -c | psql` swallows that as a short restore with
#     a nonzero exit somewhere in a pipe nobody checks. Two minutes here beats
#     discovering it at 3am.
gunzip -t "$DUMP" || { echo "CORRUPT/TRUNCATED: $DUMP — do not drill this; try offsite"; exit 1; }

# 2. Timed restore into a scratch DB (never touch the live DB):
time (createdb robothor_restore_drill && \
      gunzip -c "$DUMP" | psql -q -d robothor_restore_drill -v ON_ERROR_STOP=0 \
      2> /tmp/restore-errors.log)

# 3. Verify: table count and spot row-counts vs live (drift since the dump
#    hour is expected):
psql -d robothor_restore_drill -tAc "SELECT
  (SELECT count(*) FROM memory_facts),
  (SELECT count(*) FROM agent_runs),
  (SELECT count(*) FROM information_schema.tables WHERE table_schema='public');"

# 4. Clean up:
dropdb robothor_restore_drill
```

Drilling from the offsite copy? `gunzip -t` it there too — the fetch is the
step that can truncate, and `rclone copy` reporting success is not a CRC.

## Why the marker, and not `systemctl status`

The four backup units carry an `ExecCondition=`
(`scripts/backup-volume-check.sh`). When the volume is unusable they are
**skipped**, not failed: `Result=exec-condition`, no `OnFailure=`, no page.
That is the right behaviour — it is what ended a 96-failures-a-day page
storm — but it means **a clean `systemctl status` is not evidence a backup
ran**. The markers are the signal that survives a skip:

| Marker | Written by | Answers |
|---|---|---|
| `last-local-dump` | `scripts/backup-ssd.sh` | is there a dump to drill from, and how old |
| `last-offsite-ok` | `scripts/backup-offsite.sh` (replication runs only — a verify-only run uploads nothing and does not stamp) | would a box loss be survivable, and from which generation |
| `last-basebackup` | `scripts/pg-basebackup.sh` | how much WAL a PITR would have to replay |
| `last-wal-offsite-ok` | `scripts/wal-offsite.sh` | the recovery point actually shipped offsite |

All four live in `ROBOTHOR_BACKUP_STATE_DIR`
(`/var/lib/robothor/backup-state`) on NVMe. Same format: `<date -Is>
<identifier>`. `docs/runbooks/PAGING.md` quotes them in the consequence line of
every backup page, so what you read here is what an operator reads on their
phone.

## Measured baselines

| Date | Dump | Size (gz) | Duration | Errors | Verification |
|------|------|-----------|----------|--------|--------------|
| 2026-07-13 | robothor_memory-20260713 | 1.1 GB | **9m01s** | 0 | 92/92 tables; memory_facts/agent_runs/crm_tasks counts consistent with 17h drift |
| 2026-08-24 | robothor_memory-20260824 **from the offsite copy** | 1.3 GB | **6m00s** (+21s rclone fetch) | 0 | 116/116 tables; counts consistent with 6h drift; offsite object byte-identical to local |

Prefer drilling **from the offsite copy** (rclone fetch first): it exercises
the only path that matters in a box-loss, and the 2026-08-24 drill did exactly
that — hours after the local SSD had physically disconnected, which is the
scenario in miniature.

RPO today is ~24h (nightly dump). The hardening program (#176) targets 15-min
RPO / 60-min RTO via snapshot + WAL archiving; a 9-minute single-DB restore
fits comfortably inside that RTO budget, with the box rebuild itself as the
dominant cost.

Known gap: the `default` database (retired Buddy-era tables, ~8 MB) is not in
the nightly dump loop — drop it or add it to `scripts/backup-ssd.sh`.
