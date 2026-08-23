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
# The rclone detail lands here. It used to default INSIDE the git working tree
# (scripts/backup-offsite.log) where it was gitignored, unrotated, and — most
# of all — somewhere the paged operator had no reason to look. /var/log is
# where an operator looks. Fall back to the old spot if that is unavailable, so
# a fresh checkout on a box without /var/log/robothor still logs somewhere.
_default_log=/var/log/robothor/backup-offsite.log
if [[ -z "${ROBOTHOR_OFFSITE_LOG:-}" ]] \
   && ! { mkdir -p /var/log/robothor 2>/dev/null && touch "$_default_log" 2>/dev/null; }; then
    _default_log="$HOME/robothor/scripts/backup-offsite.log"
fi
LOG="${ROBOTHOR_OFFSITE_LOG:-$_default_log}"

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

# ── Verification ────────────────────────────────────────────────────────────
# "Missing offsite" and "the bytes differ" are different emergencies, and the
# page has to say which. Collapsing both into "verification MISMATCH" is what
# made the 2026-08-23 page unreadable: a benign retention bug looked exactly
# like data loss, so the only way to tell was to log in and read the rclone
# log by hand. rclone can split the two for us, so it does.
VERIFY_TMP=""
cleanup_verify_tmp() { [[ -n "$VERIFY_TMP" ]] && rm -rf "$VERIFY_TMP"; }
trap cleanup_verify_tmp EXIT

# Names the offending generations in the summary line itself. `fail` is what
# reaches Telegram (via send_failure_alert.sh, which tails the journal), so a
# detail that only lands in $LOG is a detail the paged operator does not have.
verify_offsite() {
    local context="$1"
    VERIFY_TMP="$(mktemp -d)"
    local missing="$VERIFY_TMP/missing" differ="$VERIFY_TMP/differ" errors="$VERIFY_TMP/errors"
    : >"$missing"; : >"$differ"; : >"$errors"

    if rclone check "$SOURCE" "$REMOTE/db" "${include_args[@]}" \
            --one-way --checkers 4 \
            --missing-on-dst "$missing" --differ "$differ" --error "$errors" \
            >>"$LOG" 2>&1; then
        log "verification OK — all ${#keep_files[@]} retained generations match the source"
        return 0
    fi

    local -a problems=()
    if [[ -s "$missing" ]]; then
        problems+=("$(wc -l <"$missing" | tr -d ' ') MISSING offsite: $(paste -sd' ' <"$missing")")
    fi
    if [[ -s "$differ" ]]; then
        problems+=("$(wc -l <"$differ" | tr -d ' ') CORRUPT offsite (bytes differ from source): $(paste -sd' ' <"$differ")")
    fi
    if [[ -s "$errors" ]]; then
        problems+=("$(wc -l <"$errors" | tr -d ' ') UNREADABLE: $(paste -sd' ' <"$errors")")
    fi
    if ((${#problems[@]} == 0)); then
        # rclone failed without naming a file — a transport or auth problem,
        # not a statement about the data. Do not imply the backup is bad.
        problems+=("rclone check failed without identifying a file — see $LOG")
    fi

    local IFS='; '
    fail "$context — ${problems[*]}"
}

# ── Verify-only: confirm what is already offsite is intact ──────────────────
if [[ "$VERIFY_ONLY" == "1" ]]; then
    log "verifying ${#keep_files[@]} retained generations offsite at $REMOTE"
    verify_offsite "offsite verification FAILED"
    exit 0
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
verify_offsite "post-upload verification FAILED — the offsite copy is not intact"

# ── Retention: keep the newest N generations offsite ────────────────────────
# The prune and the verify MUST agree on what "the retained set" means. They
# did not, and it cost a generation a night.
#
# 2026-08-23: the remote held robothor_memory-prereboot-20260714.sql.gz, put
# there by hand before a July reboot; its local copy had since been reaped at
# -mtime +30. The old prune sorted every remote *.sql.gz and deleted the lowest
# `excess`. 'p' (0x70) sorts after '2' (0x32), so the orphan was never a
# candidate — it permanently occupied a retention slot and forced the deletion
# of the OLDEST REAL generation, every single night, self-perpetuating. The
# weekly verify then demanded exactly the file the nightly run had deleted.
#
# Two rules now, and only these two:
#   * an object that is not a generation is never a retention slot and never a
#     prune victim. A human deliberately put it there and the local copy may be
#     gone; deleting it would destroy the only one. Say it out loud instead.
#   * a generation is pruned only when it is OLDER than the oldest one we are
#     keeping. Never "the lowest N by sort order" — that deletes whatever
#     happens to sort first, including things newer than the retained window if
#     the local source is ever short.
GENERATION_RE='^robothor_memory-[0-9]{8}\.sql\.gz$'

declare -A keep_set=()
for f in "${keep_files[@]}"; do keep_set["$f"]=1; done
oldest_keep="${keep_files[0]}"

mapfile -t remote_dumps < <(rclone lsf "$REMOTE/db" --include "*.sql.gz" 2>/dev/null | sort)

prune_list=()
unrecognized=()
for name in "${remote_dumps[@]}"; do
    if [[ ! "$name" =~ $GENERATION_RE ]]; then
        unrecognized+=("$name")
        continue
    fi
    [[ -n "${keep_set[$name]:-}" ]] && continue
    # Lexicographic == chronological for robothor_memory-YYYYMMDD.sql.gz.
    [[ "$name" < "$oldest_keep" ]] && prune_list+=("$name")
done

if ((${#unrecognized[@]} > 0)); then
    log "NOTE: ${#unrecognized[@]} unrecognized object(s) offsite, left untouched and not counted against retention: ${unrecognized[*]}"
fi

pruned=0
for name in "${prune_list[@]}"; do
    log "pruning old offsite generation: $name"
    if rclone deletefile "$REMOTE/db/$name" >>"$LOG" 2>&1; then
        pruned=$((pruned + 1))
    else
        log "WARNING: could not prune $name"
    fi
done

# Count what is actually there now, not what was there before the prune — the
# old line reported the pre-prune total and read as an off-by-one every night.
remaining=$(rclone lsf "$REMOTE/db" --include "*.sql.gz" 2>/dev/null | wc -l | tr -d ' ')
log "offsite replication OK ($remaining object(s) offsite, ${#keep_files[@]} retained generations, keeping $KEEP, pruned $pruned)"
