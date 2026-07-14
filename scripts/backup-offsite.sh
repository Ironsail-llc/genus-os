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

# ── Which generations we intend to retain ───────────────────────────────────
# Both the upload and the verification must operate on the same set. Verifying
# the WHOLE source against a remote that only holds KEEP generations reports
# every older local dump as a "difference" and fails on a perfectly healthy
# backup — a weekly false alarm the operator would learn to ignore, which is
# how a real backup failure gets missed.
mapfile -t keep_files < <(
    find "$SOURCE" -maxdepth 1 -name "*.sql.gz" -printf "%f\n" 2>/dev/null | sort | tail -n "$KEEP"
)
if ((${#keep_files[@]} == 0)); then
    fail "no *.sql.gz dumps found in $SOURCE"
fi

include_args=()
for f in "${keep_files[@]}"; do
    include_args+=(--include "$f")
done

# ── Verify-only: confirm what is already offsite is intact ──────────────────
if [[ "$VERIFY_ONLY" == "1" ]]; then
    log "verifying ${#keep_files[@]} retained generations offsite at $REMOTE"
    if rclone check "$SOURCE" "$REMOTE/db" "${include_args[@]}" --one-way --checkers 4 >>"$LOG" 2>&1; then
        log "verification OK — offsite copy matches source"
        exit 0
    fi
    fail "verification MISMATCH — the offsite copy does not match the source"
fi

# ── Replicate the database dumps ────────────────────────────────────────────
# Upload ONLY the generations we intend to retain. Copying the whole directory
# and pruning afterwards means shipping (and paying for) dumps that are deleted
# minutes later — at ~1.1 GB and ~4.5 MB/s per dump that is roughly 45 wasted
# minutes a night on a 17-dump source.
log "replicating ${#keep_files[@]} newest dumps (of $(find "$SOURCE" -maxdepth 1 -name '*.sql.gz' | wc -l)): $SOURCE -> $REMOTE/db"
rclone copy "$SOURCE" "$REMOTE/db" "${include_args[@]}" --transfers 2 --checkers 4 >>"$LOG" 2>&1 \
    || fail "rclone copy of database dumps failed"

# ── Preserve the guardrail posture (it lives in /etc, not in git) ───────────
if [[ -d "$DROPIN_DIR" ]]; then
    log "replicating systemd drop-ins: $DROPIN_DIR -> $REMOTE/systemd"
    rclone copy "$DROPIN_DIR" "$REMOTE/systemd" --include "*.conf" >>"$LOG" 2>&1 \
        || fail "rclone copy of systemd drop-ins failed"
fi

# ── Verify the copy landed intact before trusting it ────────────────────────
log "verifying replicated dumps"
rclone check "$SOURCE" "$REMOTE/db" "${include_args[@]}" --one-way --checkers 4 >>"$LOG" 2>&1 \
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
