#!/usr/bin/env bash
# shellcheck shell=bash
# Last-good markers: "when did this backup job last actually work?"
#
# WHY
#   Until now the only machine-readable signal a backup job produced was its
#   exit status, so "is the backup healthy?" was answered by "has a unit failed
#   recently?". That answer stops existing the moment a wedged volume makes the
#   units SKIP instead of fail (see scripts/backup-volume-check.sh) — and it was
#   never a good answer anyway: a timer that stops firing at all fails nothing.
#
#   Each job therefore stamps a marker on the LAST line of a successful run.
#   A freshness guard reads them and pages once on "it has been N hours since a
#   good local dump", instead of the old behaviour of paging every 15 minutes
#   with a unit name.
#
# WHERE
#   ${ROBOTHOR_BACKUP_STATE_DIR:-/var/lib/robothor/backup-state} — on NVMe, on
#   purpose. The disk that breaks must not be the disk that holds the evidence
#   of when it last worked; a marker on the backup volume disappears exactly
#   when it is needed.
#
# MARKERS
#   last-local-dump       scripts/backup-ssd.sh
#   last-offsite-ok       scripts/backup-offsite.sh   (a replication run;
#                                                      verify-only runs upload
#                                                      nothing and do not stamp)
#   last-wal-offsite-ok   scripts/wal-offsite.sh
#   last-basebackup       scripts/pg-basebackup.sh
#
# Usage (source it):
#   source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup-state.sh"
#   backup_state_record last-offsite-ok    # stamp "now"; NEVER fails the caller
#   backup_state_last   last-offsite-ok    # echo the stamp, or the fallback
#                                          # "unknown (no successful run
#                                          # recorded)" with a non-zero status

BACKUP_STATE_UNKNOWN="unknown (no successful run recorded)"

backup_state_dir() {
    printf '%s' "${ROBOTHOR_BACKUP_STATE_DIR:-/var/lib/robothor/backup-state}"
}

# Stamp a marker with the current UTC time.
#
# This is bookkeeping, so it ALWAYS returns 0: a backup that genuinely
# succeeded must never be reported as failed because /var/lib was read-only.
# The failure is loud in the log instead.
backup_state_record() {
    local name="${1:-}"
    if [[ -z "$name" ]]; then
        echo "backup-state: backup_state_record needs a marker name" >&2
        return 0
    fi

    local dir
    dir="$(backup_state_dir)"
    if ! mkdir -p "$dir" 2>/dev/null; then
        echo "backup-state: cannot create $dir — not recording $name" >&2
        return 0
    fi
    if ! printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$dir/$name" 2>/dev/null; then
        echo "backup-state: cannot write $dir/$name" >&2
    fi
    return 0
}

# Echo a marker's timestamp.
#
# An absent or empty marker echoes "unknown (no successful run recorded)" and
# returns 1, so a caller can branch on the status rather than string-matching —
# and so a marker that was never written can never be mistaken for a fresh one
# by a reader that only checked for a non-empty string.
backup_state_last() {
    local name="${1:-}"
    if [[ -z "$name" ]]; then
        echo "backup-state: backup_state_last needs a marker name" >&2
        printf '%s\n' "$BACKUP_STATE_UNKNOWN"
        return 1
    fi

    local file value=""
    file="$(backup_state_dir)/$name"
    if [[ -r "$file" ]]; then
        value="$(head -n 1 "$file" 2>/dev/null || true)"
    fi
    value="${value//[[:space:]]/}"

    if [[ -z "$value" ]]; then
        printf '%s\n' "$BACKUP_STATE_UNKNOWN"
        return 1
    fi
    printf '%s\n' "$value"
    return 0
}
