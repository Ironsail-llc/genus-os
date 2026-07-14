#!/usr/bin/env bash
# Postgres archive_command — the WAL side of PITR.
#
# Called by Postgres for EVERY completed WAL segment, as:
#     wal-archive.sh %p %f
#
# THIS SCRIPT IS LOAD-BEARING FOR THE DATABASE STAYING UP.
#
# If archive_command keeps failing, Postgres does NOT drop the segment — it
# retains it in pg_wal and retries forever. pg_wal grows without bound until the
# filesystem fills, and then the database STOPS. A broken archive command is a
# slow-motion outage, and it is the single most common way PITR takes production
# down.
#
# Two rules follow, and this script exists to obey them:
#
#   1. Archive to somewhere that does not disappear. NOT the USB backup volume —
#      that disk physically dropped off the bus on 2026-07-14 mid-write. It goes
#      to the local NVMe, which is also where the database lives: if that disk is
#      gone we have bigger problems than the archive.
#
#   2. NEVER exit non-zero for a reason that will not clear itself. Postgres will
#      retry forever, so a permanent failure (bad path, no permission) must be
#      loud rather than silently accumulating WAL.
#
# Offsite replication of the archive is a SEPARATE job (wal-offsite.sh) precisely
# so a network hiccup can never wedge the database.
set -euo pipefail

SRC="${1:?usage: wal-archive.sh <%p path> <%f filename>}"
DEST_NAME="${2:?usage: wal-archive.sh <%p path> <%f filename>}"

ARCHIVE_DIR="${ROBOTHOR_WAL_ARCHIVE_DIR:-/var/lib/postgresql/wal_archive}"

# A missing archive dir is a permanent failure — Postgres would retry forever and
# fill the disk. Fail immediately and visibly rather than quietly.
if [[ ! -d "$ARCHIVE_DIR" ]]; then
    echo "wal-archive: archive dir $ARCHIVE_DIR does not exist" >&2
    exit 1
fi

# Refuse to archive onto a nearly-full filesystem. Better to fail one segment
# loudly (and page) than to fill the disk the DATABASE is running on.
AVAIL_MB=$(df --output=avail -m "$ARCHIVE_DIR" | tail -1 | tr -d ' ')
MIN_FREE_MB="${ROBOTHOR_WAL_MIN_FREE_MB:-5120}"
if [[ "$AVAIL_MB" -lt "$MIN_FREE_MB" ]]; then
    echo "wal-archive: only ${AVAIL_MB}MB free at $ARCHIVE_DIR (need ${MIN_FREE_MB}MB)" >&2
    exit 1
fi

DEST="$ARCHIVE_DIR/$DEST_NAME"

# Already archived: succeed. Postgres may legitimately re-request a segment (e.g.
# after a crash) and re-copying over a good file risks a torn read on restore.
if [[ -f "$DEST" ]]; then
    exit 0
fi

# Copy to a temp name in the SAME directory, fsync, then rename. A rename within
# a filesystem is atomic, so a restore can never see a half-written segment —
# which would be a silently corrupt recovery.
TMP="$DEST.part.$$"
trap 'rm -f "$TMP"' EXIT

cp "$SRC" "$TMP"
# Durability: the whole point of an archive is surviving the crash that ate pg_wal.
sync -d "$TMP" 2>/dev/null || sync
# Group-readable: Postgres writes these as `postgres`, but the offsite replication
# job runs as the operator (rclone's credentials live in the operator's home, and
# postgres cannot read them). Without this the segments are 0600 and the offsite
# copy fails with "permission denied" — WAL archived locally and never leaving the
# box, which is a backup that does not survive losing the box.
chmod 0640 "$TMP"
mv "$TMP" "$DEST"
trap - EXIT

exit 0
