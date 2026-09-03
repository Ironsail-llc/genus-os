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
# FORMAT
#   One line: "<date -Is> <identifier>", e.g.
#
#     2026-09-02T04:30:11+02:00 robothor_memory-20260902.sql.gz
#
#   The timestamp carries its UTC offset, so it stays orderable against `now`
#   even if the box changes zone — a bare local time reads as hours of drift.
#
#   The identifier is what the run actually produced: the dump filename, the
#   offsite object, the base backup directory, the newest WAL segment. Without
#   it a freshness page can only say "offsite is 40 hours stale" and the
#   operator has to log in to find out which generation that means.
#
# Usage (source it):
#   source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/backup-state.sh"
#   backup_state_mark    last-offsite-ok OBJECT  # stamp; NEVER fails the caller
#   backup_state_last    last-offsite-ok         # the whole line, or the
#                                                # fallback "unknown (no
#                                                # successful run recorded)"
#                                                # with a non-zero status
#   backup_state_last_ts last-offsite-ok         # just the timestamp

BACKUP_STATE_UNKNOWN="unknown (no successful run recorded)"

backup_state_dir() {
    printf '%s' "${ROBOTHOR_BACKUP_STATE_DIR:-/var/lib/robothor/backup-state}"
}

# Stamp a marker: when this job last worked, and what it produced.
#
#   backup_state_mark last-local-dump robothor_memory-20260902.sql.gz
#
# This is bookkeeping, so it ALWAYS returns 0: a backup that genuinely
# succeeded must never be reported as failed because /var/lib was read-only.
# The failure is loud in the log instead — including a missing identifier,
# which is a wiring bug in the caller and not a reason to fail its backup.
backup_state_mark() {
    local name="${1:-}"
    local identifier="${2:-}"
    if [[ -z "$name" ]]; then
        echo "backup-state: backup_state_mark needs a marker name" >&2
        return 0
    fi
    if [[ -z "$identifier" ]]; then
        echo "backup-state: backup_state_mark $name has no identifier" >&2
        identifier="-"
    fi

    local dir
    dir="$(backup_state_dir)"
    if ! mkdir -p "$dir" 2>/dev/null; then
        echo "backup-state: cannot create $dir — not recording $name" >&2
        return 0
    fi
    if ! printf '%s %s\n' "$(date -Is)" "$identifier" >"$dir/$name" 2>/dev/null; then
        echo "backup-state: cannot write $dir/$name" >&2
    fi
    return 0
}

# Echo a marker's whole line: "<timestamp> <identifier>".
#
# An absent or empty marker echoes "unknown (no successful run recorded)" and
# returns 1, so a caller can branch on the status rather than string-matching —
# and so a marker that was never written can never be mistaken for a fresh one
# by a reader that only checked for a non-empty string.
#
# Only surrounding whitespace is trimmed. This used to strip ALL whitespace,
# which glued the timestamp and the identifier into one unparseable token the
# moment the marker grew a second field.
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
    # Leading, then trailing (\r included: a marker restored from a backup that
    # passed through a Windows box must not read as a different timestamp).
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ -z "$value" ]]; then
        printf '%s\n' "$BACKUP_STATE_UNKNOWN"
        return 1
    fi
    printf '%s\n' "$value"
    return 0
}

# Echo just the timestamp — field 1 — for a guard doing date arithmetic.
# Same unknown/non-zero contract as backup_state_last.
backup_state_last_ts() {
    local line
    if ! line="$(backup_state_last "${1:-}")"; then
        printf '%s\n' "$line"
        return 1
    fi
    printf '%s\n' "${line%% *}"
    return 0
}
