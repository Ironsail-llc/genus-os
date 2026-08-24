# Database Restore Drill

Backups that have never been restored are hope, not backups. This runbook is
the drill procedure plus the measured baseline; re-run it quarterly (or after
any change to the backup pipeline) and update the table below.

## Procedure

```bash
# 1. Latest nightly dump (produced by scripts/backup-ssd.sh at 04:30).
#    GUARD THE GLOB: on 2026-08-24 the backup SSD had USB-disconnected and the
#    glob matched NOTHING — and the drill pipeline below then "succeeded" in
#    0.09s against an empty database. An empty dump variable must abort.
DUMP=$(ls -t /mnt/robothor-backup/robothor/db/robothor_memory-*.sql.gz 2>/dev/null | head -1)
[ -n "$DUMP" ] || { echo "NO LOCAL DUMP — mount gone? Drill from offsite instead:"; \
                    echo "  rclone copy <remote>/db/<newest>.sql.gz /tmp/"; exit 1; }

# 2. Timed restore into a scratch DB (never touch the live DB):
time (createdb robothor_restore_drill && \
      gunzip -c <dump> | psql -q -d robothor_restore_drill -v ON_ERROR_STOP=0 \
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
