#!/usr/bin/env bash
# Install the host ops scripts (WAL archive, thermal, boot and GPU guards)
# from the repo into their live location, replacing the hand-copy workflow.
#
# Only scripts something actually EXECUTES from /usr/local/bin belong here.
# The base-backup and WAL-offsite jobs run their workspace copy (see their
# units' ExecStart=) and source sibling helpers, so a mirror here cannot run
# at all; the stale ones are removed below.
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

# NOTE: the doctor (scripts/instance_doctor.sh) and guardrail_watch.py derive
# what to drift-check from these `install_one` lines, so a line removed here
# removes its check too. Keep the literal two-argument form.
install_one "${REPO_ROOT}/scripts/wal-archive.sh" "robothor-wal-archive.sh"
install_one "${REPO_ROOT}/scripts/thermal-guard.sh" "robothor-thermal-guard.sh"
install_one "${REPO_ROOT}/scripts/thermal-shed.sh" "robothor-thermal-shed.sh"
install_one "${REPO_ROOT}/scripts/boot-guard.sh" "robothor-boot-guard.sh"
install_one "${REPO_ROOT}/scripts/gpu-clock-cap.sh" "robothor-gpu-clock-cap.sh"

# ── Retired mirrors ───────────────────────────────────────────────────────────
# pg-basebackup.sh and wal-offsite.sh were mirrored here and never invoked:
# robothor-basebackup.service and robothor-wal-offsite.service both ExecStart
# the WORKSPACE copy, and always did. The mirrors were dead weight that the
# drift check nevertheless kept comparing.
#
# They are now worse than dead. Both scripts `source "$SCRIPT_DIR/backup-state.sh"`
# (wal-offsite.sh also needs backup-volume-check.sh), and /usr/local/bin has no
# sibling of that name — so a mirror aborts on its first source line while
# looking, to anyone reading the directory, exactly like the installed backup.
# Copying the helpers alongside would create a second, parallel backup
# implementation to keep in sync; deleting the unused copy will not.
#
# Removal is unconditional and logged rather than left to the operator: the old
# installer put these on every box that ran it, and a broken script nobody
# deletes is the same trap the next reader falls into.
for stale_name in robothor-pg-basebackup.sh robothor-wal-offsite.sh; do
    stale="${BIN_DIR}/${stale_name}"
    [[ -e "$stale" ]] || continue
    rm -f "$stale"
    log "removed ${stale} — no unit ran it, and it sources a sibling that only exists in the workspace"
done

# ── Log rotation ──────────────────────────────────────────────────────────────
# /etc/logrotate.d/robothor existed on the box with no source in the repo and
# covered one glob, so brain/memory_system/logs/ reached 205 MB unrotated. The
# config is a TEMPLATE (the workspace differs per instance) and goes through
# the same renderer as the systemd units.
#
# A render failure is FATAL, deliberately: the alternative is an installer that
# reports success while leaving the box with no rotation policy — and the whole
# reason this file exists is that nothing ever noticed the last such gap. It
# runs after the ops scripts so a missing render env cannot also cost the box
# its backup and thermal scripts.
LOGROTATE_SRC="${REPO_ROOT}/infra/logrotate/robothor.conf"
LOGROTATE_DST="${ROOT}/etc/logrotate.d/robothor"
if [[ -f "$LOGROTATE_SRC" ]]; then
    RENDER="${REPO_ROOT}/scripts/render-unit.sh"
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    if ! bash "$RENDER" "$LOGROTATE_SRC" "${TMP_DIR}/robothor"; then
        log "ERROR: could not render ${LOGROTATE_SRC} — /etc/logrotate.d/robothor NOT installed."
        log "ERROR: Set ROBOTHOR_WORKSPACE (and ROBOTHOR_SERVICE_USER), or provide"
        log "ERROR: /etc/robothor/robothor.env. Logs will grow without bound until this is fixed."
        exit 1
    fi
    install -d -m 0755 "$(dirname "$LOGROTATE_DST")"
    if [[ ! -f "$LOGROTATE_DST" ]]; then
        install -m 0644 "${TMP_DIR}/robothor" "$LOGROTATE_DST"
        log "installed ${LOGROTATE_DST}"
    elif ! cmp -s "${TMP_DIR}/robothor" "$LOGROTATE_DST"; then
        install -m 0644 "${TMP_DIR}/robothor" "$LOGROTATE_DST"
        log "updated ${LOGROTATE_DST}"
    else
        chmod 0644 "$LOGROTATE_DST"
        log "unchanged ${LOGROTATE_DST}"
    fi
else
    log "WARNING: ${LOGROTATE_SRC} missing — no log rotation policy installed."
fi

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
