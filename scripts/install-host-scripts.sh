#!/usr/bin/env bash
# Install the host ops scripts (base backup, WAL offsite, WAL archive) from
# the repo into their live location, replacing the hand-copy workflow.
#
# Today's incident's root cause: these scripts are hand-copied to
# /usr/local/bin/robothor-*.sh with no installer and no drift check. A
# permission fix in scripts/pg-basebackup.sh sat in the repo for a month
# because nothing ever copied it over, and scripts/guardrail_watch.py had
# nothing to compare the stale installed copy against.
#
# Usage: install-host-scripts.sh [--root DIR] [--group NAME]
#   --root DIR    filesystem root to install under (default /, so scripts
#                  land at /usr/local/bin/robothor-*.sh; override for tests)
#   --group NAME  offsite backup group to check postgres's membership in
#                  (default: $ROBOTHOR_BACKUP_GROUP)
#
# Idempotent: re-running reports "unchanged" for files that already match,
# and only rewrites the ones that don't.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT=""
GROUP="${ROBOTHOR_BACKUP_GROUP:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            ROOT="${2:?--root requires a directory}"
            shift 2
            ;;
        --group)
            GROUP="${2:?--group requires a name}"
            shift 2
            ;;
        *)
            echo "usage: install-host-scripts.sh [--root DIR] [--group NAME]" >&2
            exit 1
            ;;
    esac
done

BIN_DIR="${ROOT}/usr/local/bin"
mkdir -p "$BIN_DIR"

log() { echo "[install-host-scripts] $*"; }

install_one() {
    local src="$1" dest_name="$2"
    local dest="${BIN_DIR}/${dest_name}"
    if [[ ! -f "$dest" ]]; then
        install -m 0755 "$src" "$dest"
        log "installed ${dest}"
    elif ! cmp -s "$src" "$dest"; then
        install -m 0755 "$src" "$dest"
        log "updated ${dest}"
    else
        chmod 0755 "$dest"
        log "unchanged ${dest}"
    fi
}

install_one "${REPO_ROOT}/scripts/pg-basebackup.sh" "robothor-pg-basebackup.sh"
install_one "${REPO_ROOT}/scripts/wal-offsite.sh" "robothor-wal-offsite.sh"
install_one "${REPO_ROOT}/scripts/wal-archive.sh" "robothor-wal-archive.sh"
install_one "${REPO_ROOT}/scripts/thermal-guard.sh" "robothor-thermal-guard.sh"
install_one "${REPO_ROOT}/scripts/thermal-shed.sh" "robothor-thermal-shed.sh"
install_one "${REPO_ROOT}/scripts/boot-guard.sh" "robothor-boot-guard.sh"
install_one "${REPO_ROOT}/scripts/gpu-clock-cap.sh" "robothor-gpu-clock-cap.sh"

# ── Group-membership check ────────────────────────────────────────────────────
# Today's live incident's root cause: pg-basebackup.sh runs as `postgres` and
# its `chgrp $ROBOTHOR_BACKUP_GROUP` + `chmod 2775` SILENTLY fail — and Linux
# STRIPS the setgid bit on a chmod by a non-member — when postgres is not
# actually a member of that group. The base backup then never leaves the box
# for the offsite job to pick up, with nothing to say why. Check it here,
# once, at install time, rather than relying on someone reading the backup
# job's logs.
if [[ -n "$GROUP" ]]; then
    if id "postgres" >/dev/null 2>&1; then
        if ! id -nG postgres 2>/dev/null | tr ' ' '\n' | grep -qx "$GROUP"; then
            log "WARNING: postgres is NOT a member of group '${GROUP}'."
            log "WARNING: pg-basebackup.sh's chgrp/chmod will silently fail, and base"
            log "WARNING: backups will never leave the box for the offsite job. Fix:"
            log "WARNING:   sudo usermod -aG ${GROUP} postgres"
        fi
    else
        log "postgres user not found (ok under --root) — skipping group-membership check"
    fi
fi

log "done"
