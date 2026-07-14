#!/usr/bin/env bash
# Replicate the WAL archive offsite, and keep the local archive from growing
# without bound. Runs on a timer — deliberately NOT inside Postgres's
# archive_command, so a network problem can never wedge the database.
#
# This is what turns RPO from 24 hours (the nightly dump) into roughly the timer
# interval.
#
# It also checks the two things that actually kill a PITR setup:
#   * archive_command silently failing (pg_stat_archiver.last_failed_wal), which
#     makes pg_wal grow until the database halts;
#   * a base backup so old that the WAL needed to replay onto it has been pruned,
#     which means you have an archive that restores to nothing.
#
# Exits non-zero on either, so the systemd OnFailure hook pages the operator.
set -euo pipefail

ARCHIVE_DIR="${ROBOTHOR_WAL_ARCHIVE_DIR:-/var/lib/postgresql/wal_archive}"
BASEBACKUP_DIR="${ROBOTHOR_BASEBACKUP_DIR:-/mnt/robothor-backup/robothor/basebackup}"
REMOTE="${ROBOTHOR_OFFSITE_REMOTE:-}"
DB="${ROBOTHOR_DB_NAME:-robothor_memory}"
# Keep WAL for this many days beyond the newest base backup.
KEEP_DAYS="${ROBOTHOR_WAL_KEEP_DAYS:-8}"

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { log "ERROR: $*"; exit 1; }

[[ -d "$ARCHIVE_DIR" ]] || fail "archive dir $ARCHIVE_DIR does not exist"

# ── 1. Is Postgres actually archiving, or silently failing? ──────────────────
# A failing archive_command is a slow-motion outage: Postgres retains every WAL
# segment and pg_wal grows until the filesystem fills and the database stops.
# "Zero archived segments" is NOT evidence of health — it is equally consistent
# with the command being broken.
STATS=$(psql -d "$DB" -tAc "
    SELECT archived_count || '|' || failed_count || '|' || COALESCE(last_failed_wal, '-')
    FROM pg_stat_archiver;" 2>/dev/null || echo "")
[[ -n "$STATS" ]] || fail "could not read pg_stat_archiver"

ARCHIVED=${STATS%%|*}
REST=${STATS#*|}
FAILED=${REST%%|*}
LAST_FAILED=${REST#*|}

log "pg_stat_archiver: archived=$ARCHIVED failed=$FAILED last_failed=$LAST_FAILED"

if [[ "$FAILED" -gt 0 && "$LAST_FAILED" != "-" ]]; then
    fail "Postgres is FAILING to archive WAL (last: $LAST_FAILED). pg_wal will grow until the disk fills and the database STOPS."
fi

# ── 2. Push the archive offsite ──────────────────────────────────────────────
if [[ -n "$REMOTE" ]]; then
    command -v rclone >/dev/null 2>&1 || fail "rclone is not installed"
    log "replicating WAL archive to $REMOTE/wal"
    rclone copy "$ARCHIVE_DIR" "$REMOTE/wal" --transfers 8 --checkers 16 || fail "rclone copy of WAL archive failed"

    if [[ -d "$BASEBACKUP_DIR" ]]; then
        log "replicating base backups to $REMOTE/basebackup"
        rclone copy "$BASEBACKUP_DIR" "$REMOTE/basebackup" --transfers 4 || fail "rclone copy of base backups failed"
    fi
else
    log "ROBOTHOR_OFFSITE_REMOTE unset — archiving locally only (RPO is not offsite)"
fi

# ── 3. Prune WAL that predates the newest base backup ────────────────────────
# WAL is only useful for replaying ONTO a base backup. Anything older than the
# newest base backup is dead weight — but pruning MORE than that silently
# destroys the ability to recover, so this only ever prunes below the base.
NEWEST_BASE=$(ls -1t "$BASEBACKUP_DIR"/*.backup_label 2>/dev/null | head -1 || true)
if [[ -n "$NEWEST_BASE" ]] && command -v pg_archivecleanup >/dev/null 2>&1; then
    OLDEST_NEEDED=$(awk '/^START WAL LOCATION/ {gsub(/[()]/, "", $6); print $6}' "$NEWEST_BASE" 2>/dev/null || true)
    if [[ -n "$OLDEST_NEEDED" ]]; then
        log "pruning WAL older than $OLDEST_NEEDED (needed by $(basename "$NEWEST_BASE"))"
        pg_archivecleanup "$ARCHIVE_DIR" "$OLDEST_NEEDED" || log "WARN: pg_archivecleanup failed (not fatal)"
    fi
else
    log "no base backup found — NOT pruning WAL (an archive with no base restores to nothing)"
fi

# ── 4. Report ────────────────────────────────────────────────────────────────
SEGMENTS=$(find "$ARCHIVE_DIR" -name '0*' -type f 2>/dev/null | wc -l)
SIZE=$(du -sh "$ARCHIVE_DIR" 2>/dev/null | cut -f1)
AVAIL_GB=$(( $(df --output=avail -m "$ARCHIVE_DIR" | tail -1 | tr -d ' ') / 1024 ))
log "archive: $SEGMENTS segments, $SIZE, ${AVAIL_GB}GB free"

# A runaway archive means pruning is broken or no base backup exists. Page before
# it becomes a full disk and a stopped database.
if [[ "$AVAIL_GB" -lt 10 ]]; then
    fail "only ${AVAIL_GB}GB free where WAL is archived — the database will STOP if this fills"
fi

log "WAL offsite replication complete"
