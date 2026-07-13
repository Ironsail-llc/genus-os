#!/usr/bin/env bash
# Replicate the recoverable core of the instance to an offsite remote.
#
# Every backup currently lives on a disk attached to the same machine as
# production: one fire, theft, or PSU surge takes prod AND every backup. This
# pushes what you actually need to rebuild — the database dumps, the systemd
# drop-ins that carry the guardrail posture (they live in /etc, not git), and
# the instance config — to an rclone remote, verifies it landed, prunes old
# generations, and pages the operator if any of that fails. A silent backup
# failure is indistinguishable from no backup.
#
# Runs after backup-ssd.sh (see infra/systemd/robothor-backup-offsite.*).
#
# Config (env):
#   ROBOTHOR_OFFSITE_REMOTE      rclone destination, e.g. "r2:robothor-backups"
#                                (required — a plain path works too, for tests)
#   ROBOTHOR_OFFSITE_SOURCE      dump dir (default /mnt/robothor-backup/robothor/db)
#   ROBOTHOR_OFFSITE_DROPIN_DIR  systemd drop-in dir to preserve
#   ROBOTHOR_OFFSITE_KEEP        generations to retain offsite (default 7)
#   ROBOTHOR_OFFSITE_VERIFY_ONLY set to 1 to check the remote without uploading
#   ROBOTHOR_OFFSITE_LOG         log file
set -uo pipefail

REMOTE="${ROBOTHOR_OFFSITE_REMOTE:-}"
SOURCE="${ROBOTHOR_OFFSITE_SOURCE:-/mnt/robothor-backup/robothor/db}"
DROPIN_DIR="${ROBOTHOR_OFFSITE_DROPIN_DIR:-/etc/systemd/system/robothor-engine.service.d}"
KEEP="${ROBOTHOR_OFFSITE_KEEP:-7}"
VERIFY_ONLY="${ROBOTHOR_OFFSITE_VERIFY_ONLY:-0}"
LOG="${ROBOTHOR_OFFSITE_LOG:-$HOME/robothor/scripts/backup-offsite.log}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

fail() {
    log "FAILED: $*"
    # Page the operator — a backup that quietly stops running is the whole risk.
    if [[ -x "$(dirname "${BASH_SOURCE[0]}")/send_failure_alert.sh" ]]; then
        "$(dirname "${BASH_SOURCE[0]}")/send_failure_alert.sh" "offsite-backup: $*" || true
    fi
    exit 1
}

command -v rclone >/dev/null 2>&1 || fail "rclone is not installed"
[[ -n "$REMOTE" ]] || fail "ROBOTHOR_OFFSITE_REMOTE is not set — no offsite destination configured"
[[ -d "$SOURCE" ]] || fail "backup source directory not found: $SOURCE"

# ── Verify-only: confirm what is already offsite is intact ──────────────────
if [[ "$VERIFY_ONLY" == "1" ]]; then
    log "verifying offsite copy at $REMOTE"
    if rclone check "$SOURCE" "$REMOTE/db" --one-way --checkers 4 >>"$LOG" 2>&1; then
        log "verification OK — offsite copy matches source"
        exit 0
    fi
    fail "verification MISMATCH — the offsite copy does not match the source"
fi

# ── Replicate the database dumps ────────────────────────────────────────────
log "replicating dumps: $SOURCE -> $REMOTE/db"
rclone copy "$SOURCE" "$REMOTE/db" --transfers 2 --checkers 4 >>"$LOG" 2>&1 \
    || fail "rclone copy of database dumps failed"

# ── Preserve the guardrail posture (it lives in /etc, not in git) ───────────
if [[ -d "$DROPIN_DIR" ]]; then
    log "replicating systemd drop-ins: $DROPIN_DIR -> $REMOTE/systemd"
    rclone copy "$DROPIN_DIR" "$REMOTE/systemd" --include "*.conf" >>"$LOG" 2>&1 \
        || fail "rclone copy of systemd drop-ins failed"
fi

# ── Verify the copy landed intact before trusting it ────────────────────────
log "verifying replicated dumps"
rclone check "$SOURCE" "$REMOTE/db" --one-way --checkers 4 >>"$LOG" 2>&1 \
    || fail "post-upload verification failed — the offsite copy is not intact"

# ── Retention: keep the newest N generations offsite ────────────────────────
mapfile -t remote_dumps < <(rclone lsf "$REMOTE/db" --include "*.sql.gz" 2>/dev/null | sort)
excess=$(( ${#remote_dumps[@]} - KEEP ))
if (( excess > 0 )); then
    for ((i = 0; i < excess; i++)); do
        log "pruning old offsite generation: ${remote_dumps[i]}"
        rclone deletefile "$REMOTE/db/${remote_dumps[i]}" >>"$LOG" 2>&1 \
            || log "WARNING: could not prune ${remote_dumps[i]}"
    done
fi

log "offsite replication OK (${#remote_dumps[@]} generations, keeping $KEEP)"
