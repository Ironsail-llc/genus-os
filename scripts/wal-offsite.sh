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

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
VOLUME_CHECK="${ROBOTHOR_VOLUME_CHECK:-$SCRIPT_DIR/backup-volume-check.sh}"

# Last-good markers: a freshness guard needs to know when this last WORKED,
# not only whether the most recent run failed. See scripts/backup-state.sh.
# shellcheck source=scripts/backup-state.sh
source "$SCRIPT_DIR/backup-state.sh"

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { log "ERROR: $*"; exit 1; }

[[ -d "$ARCHIVE_DIR" ]] || fail "archive dir $ARCHIVE_DIR does not exist"

# ── 0. Is the backup volume usable? ──────────────────────────────────────────
# This unit runs every 15 minutes. On 2026-08-27 the encrypted USB volume went
# `emergency_ro` — stat() kept working, so the old `[[ -d "$BASEBACKUP_DIR" ]]`
# below passed, rclone and the prune ran against a dead disk, and the unit
# failed 96 times in a day. ~22 Telegram pages whose entire content was a unit
# name is a muted pager.
#
# But this unit must DEGRADE, not skip (its four sibling backup units use
# ExecCondition= to skip): the WAL archive lives on NVMe and this push IS the
# 15-minute RPO. Refusing to run because a different, USB-attached disk is
# wedged would trade a paging storm for real data loss. So the two steps that
# read the backup volume — replicating the base backups, and reading the newest
# backup_label to fix the prune horizon — are skipped, the WAL still goes
# offsite, and this exits 0.
#
# A missing probe counts as unhealthy on purpose: degrading is safe (§4's disk
# guard still pages if the unpruned archive threatens to fill the disk), while
# assuming health would put us straight back in the outage.
#
# Only exit 1 degrades. Exit 255 is the probe saying "I cannot answer the
# question" (its own tools are missing) — systemd FAILS the four sibling units
# on that, and treating it here as "the volume is down" would leave this unit
# permanently degraded and permanently silent: no basebackup replication, no
# WAL prune, no failure. So 255 degrades AND fails, which pages once per
# OnFailure cooldown instead of never.
#
# Declared before §2 so a broken probe can set it.
OFFSITE_FAILED=0
VOLUME_DOWN=0
if [[ ! -x "$VOLUME_CHECK" ]]; then
    log "ERROR: volume probe not found at $VOLUME_CHECK"
    VOLUME_DOWN=1
else
    VOLUME_CHECK_RC=0
    "$VOLUME_CHECK" --ro "$BASEBACKUP_DIR" || VOLUME_CHECK_RC=$?
    if [[ "$VOLUME_CHECK_RC" -eq 255 ]]; then
        log "ERROR: backup-volume-check.sh is broken (exit 255) — refusing to guess"
        VOLUME_DOWN=1
        OFFSITE_FAILED=1
    elif [[ "$VOLUME_CHECK_RC" -ne 0 ]]; then
        VOLUME_DOWN=1
    fi
fi
if [[ "$VOLUME_DOWN" -eq 1 ]]; then
    log "backup volume unhealthy — skipping basebackup replication and WAL prune"
fi

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
# A failure here must NOT skip §3 (prune) or §4 (disk guard) below — those are
# what keep pg_wal bounded, and a network/offsite outage is exactly when they
# matter most. So capture the failure instead of exiting, and exit at the very
# end (after §3/§4 have run) so systemd's OnFailure hook still pages.
# OFFSITE_FAILED is initialised in §0, which can already have set it.
if [[ -n "$REMOTE" ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
        log "ERROR: rclone is not installed"
        OFFSITE_FAILED=1
    else
        log "replicating WAL archive to $REMOTE/wal"
        if ! rclone copy "$ARCHIVE_DIR" "$REMOTE/wal" --transfers 8 --checkers 16; then
            log "ERROR: rclone copy of WAL archive failed"
            OFFSITE_FAILED=1
        fi

        # §0 already proved the volume answers readdir; `[[ -d ]]` alone did
        # not, which is how rclone came to be pointed at a dead disk.
        if [[ "$VOLUME_DOWN" -eq 0 ]]; then
            log "replicating base backups to $REMOTE/basebackup"
            if ! rclone copy "$BASEBACKUP_DIR" "$REMOTE/basebackup" --transfers 4; then
                log "ERROR: rclone copy of base backups failed"
                OFFSITE_FAILED=1
            fi
        fi
    fi
else
    log "ROBOTHOR_OFFSITE_REMOTE unset — archiving locally only (RPO is not offsite)"
fi

# ── 3. Prune WAL that predates the newest base backup ────────────────────────
# WAL is only useful for replaying ONTO a base backup. Anything older than the
# newest base backup is dead weight — but pruning MORE than that silently
# destroys the ability to recover, so this only ever prunes below the base.
#
# When the backup volume is down the horizon is UNKNOWABLE, so nothing is
# pruned. That is the safe direction to fail: an over-eager prune destroys the
# ability to recover, while an under-pruned archive is only a disk-space
# problem — and §4 below still pages if it becomes one.
NEWEST_BASE=""
if [[ "$VOLUME_DOWN" -eq 0 ]]; then
    NEWEST_BASE=$(ls -1t "$BASEBACKUP_DIR"/*.backup_label 2>/dev/null | head -1 || true)
fi
if [[ "$VOLUME_DOWN" -eq 1 ]]; then
    log "backup volume unhealthy — NOT pruning WAL (the prune horizon is read from the newest base backup, which is unreadable)"
elif [[ -n "$NEWEST_BASE" ]] && command -v pg_archivecleanup >/dev/null 2>&1; then
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

# Stamp the marker only when the WAL actually reached the remote. A marker
# written on a failed push makes a stale archive look fresh, which is worse
# than no marker at all. A DEGRADED run (backup volume wedged) does stamp:
# the WAL did go offsite, which is what this marker is about.
#
# $REMOTE must be non-empty. OFFSITE_FAILED stays 0 on the "archiving locally
# only" path — nothing was attempted, so nothing failed — so gating on it alone
# stamped "the WAL is offsite" every 15 minutes on an instance that has no
# offsite destination at all.
#
# The identifier is the newest WAL segment in the archive — segment names sort
# lexicographically in WAL order, so it is the exact recovery point this run
# put offsite, which is what an RPO page needs to quote.
if [[ -n "$REMOTE" && "$OFFSITE_FAILED" -eq 0 ]]; then
    NEWEST_WAL=$(find "$ARCHIVE_DIR" -maxdepth 1 -name '0*' -type f -printf '%f\n' \
        2>/dev/null | sort | tail -1)
    backup_state_mark last-wal-offsite-ok "${NEWEST_WAL:-no-segments}"
fi

# Now that the prune and disk guard have both run unconditionally, surface the
# §2 failure (if any) so OnFailure still pages the operator about it.
if [[ "$OFFSITE_FAILED" -eq 1 ]]; then
    fail "offsite replication failed earlier (see ERROR lines above) — WAL was still pruned and disk-guarded, but connectivity/rclone needs fixing"
fi
