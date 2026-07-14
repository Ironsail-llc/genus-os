#!/usr/bin/env bash
# Take a physical base backup — the thing WAL replays ONTO.
#
# The nightly `pg_dump` is a LOGICAL backup. You cannot do point-in-time recovery
# onto a pg_dump: PITR replays WAL onto a *physical* base backup, and nothing
# else. So an instance with WAL archiving but no base backup has an archive that
# restores to nothing — which looks exactly like a working PITR setup right up
# until you need it.
#
# This is the base. Weekly is enough for a ~4GB database: recovery time is
# base-restore + replay of at most a week of WAL.
set -euo pipefail

DEST="${ROBOTHOR_BASEBACKUP_DIR:-/mnt/robothor-backup/robothor/basebackup}"
KEEP="${ROBOTHOR_BASEBACKUP_KEEP:-3}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { log "ERROR: $*"; exit 1; }

# The backup volume is USB and has physically dropped off the bus before
# (2026-07-14). Fail loudly rather than writing a "base backup" into an empty
# mountpoint on the root filesystem — which would look like success.
MOUNT="${DEST%%/robothor/*}"
if ! mountpoint -q "$MOUNT" 2>/dev/null; then
    fail "$MOUNT is not mounted — refusing to write a base backup to the root filesystem"
fi

mkdir -p "$DEST"
OUT="$DEST/base-$STAMP"

# pg_basebackup writes 0700/postgres. The offsite replication job runs as the
# operator (rclone's credentials live in the operator's home; postgres cannot read
# them), so without this the base backup is archived locally and NEVER LEAVES THE
# BOX — a backup that does not survive losing the box, which is the scenario it
# exists for. setgid on the parent so future runs inherit the group.
OFFSITE_GROUP="${ROBOTHOR_BACKUP_GROUP:-}"
if [[ -n "$OFFSITE_GROUP" ]]; then
    chgrp "$OFFSITE_GROUP" "$DEST" 2>/dev/null || true
    chmod 2775 "$DEST" 2>/dev/null || true
fi

log "starting base backup -> $OUT"
# -X stream: ship the WAL generated DURING the backup with it, so the base is
#            self-consistent even before the archive is consulted.
# -c fast:   checkpoint immediately instead of waiting (this is a quiet box).
pg_basebackup \
    --pgdata="$OUT" \
    --format=tar \
    --gzip \
    --wal-method=stream \
    --checkpoint=fast \
    --progress \
    --no-password \
    || fail "pg_basebackup failed"

# The backup_label records the WAL position the base starts at. wal-offsite.sh
# reads it to know which WAL is still needed and which can be pruned. Without it,
# pruning is blind — so surface it rather than leaving it inside the tarball.
if [[ -f "$OUT/backup_manifest" ]]; then
    log "manifest present"
fi
tar -xOzf "$OUT/base.tar.gz" backup_label 2>/dev/null > "$DEST/base-$STAMP.backup_label" || \
    log "WARN: could not extract backup_label — WAL pruning will be conservative"

# Group-readable, so the offsite job can actually ship it.
chmod -R g+rX "$OUT" 2>/dev/null || true
[[ -f "$DEST/base-$STAMP.backup_label" ]] && chmod g+r "$DEST/base-$STAMP.backup_label" 2>/dev/null || true

SIZE=$(du -sh "$OUT" | cut -f1)
log "base backup complete: $SIZE"

# Keep the last N. Deleting a base backup strands the WAL that replays onto it,
# so wal-offsite.sh prunes WAL only below the NEWEST base — always in that order.
mapfile -t OLD < <(ls -1dt "$DEST"/base-* 2>/dev/null | grep -v '\.backup_label$' | tail -n +$((KEEP + 1)))
for d in "${OLD[@]:-}"; do
    [[ -n "$d" ]] || continue
    log "pruning old base backup: $(basename "$d")"
    rm -rf "$d" "${d}.backup_label"
done

log "done"
