#!/usr/bin/env bash
# Restore the newest backup into a scratch database, time it, verify it, drop
# it. The automated form of docs/runbooks/RESTORE_DRILL.md.
#
# WHY THIS IS NOT robothor-backup-verify
#   robothor-backup-verify.timer sounds like a drill and is not one: it is
#   backup-offsite.sh with ROBOTHOR_OFFSITE_VERIFY_ONLY=1, an rclone
#   byte-comparison of the local dumps against the remote. That proves the
#   bytes match. It proves nothing about whether those bytes reconstitute a
#   database — a dump truncated at source is byte-identical offsite and
#   restores into nothing.
#
#   The only question a backup has to answer is "can it be restored", and it
#   had been asked by hand twice in five months. This puts it on a timer.
#
# THE GUARD THE RUNBOOK LEARNED THE HARD WAY
#   On 2026-08-24 the backup SSD had USB-disconnected, the dump glob matched
#   NOTHING, and the drill pipeline "succeeded" in 0.09s against an empty
#   database. So: an empty dump aborts non-zero, and a restore that produces
#   ZERO tables fails even though psql exited 0. psql's exit status says only
#   that it read the file.
#
# OFFSITE FIRST
#   A box loss restores from offsite, so that is the path worth exercising —
#   the 2026-08-24 drill did exactly that, hours after the local SSD had
#   physically disconnected, which is the scenario in miniature. The local copy
#   is the fallback when the remote is unset or unreachable: a drill that skips
#   itself when the network is down is a drill that never runs.
#
# SAFETY
#   The only destructive verb here is dropdb, and it can reach exactly one
#   database: a scratch name that must contain "drill" and must not be the live
#   database or a template. Anything else is refused before a connection is
#   opened.
#
# Usage: restore-drill.sh            (no arguments; everything is env-driven)
#
# Exit: 0 the drill restored and verified a real database
#       1 the drill could not run, or the restore did not produce one
#
# Environment:
#   ROBOTHOR_RESTORE_DRILL_DB         scratch database (robothor_restore_drill)
#   ROBOTHOR_OFFSITE_REMOTE           rclone remote, shared with backup-offsite.sh
#   ROBOTHOR_RESTORE_DRILL_LOCAL_DIR  local dump dir fallback
#                                     (/mnt/robothor-backup/robothor/db)
#   ROBOTHOR_RESTORE_DRILL_WORK_DIR   where an offsite dump is fetched to
#   ROBOTHOR_RESTORE_DRILL_RCLONE_CMD rclone
#   ROBOTHOR_RESTORE_DRILL_PSQL       psql
#   ROBOTHOR_RESTORE_DRILL_CREATEDB   createdb
#   ROBOTHOR_RESTORE_DRILL_DROPDB     dropdb
#   ROBOTHOR_RESTORE_DRILL_DROP_TIMEOUT  seconds a dropdb may block (300).
#                                     dropdb waits forever on a database that
#                                     still has a backend connected
#   ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD replaces the built-in notifier; called as
#                                     CMD <subject> <body>
#   ROBOTHOR_DB_NAME                  the LIVE database, refused as a target
#   ROBOTHOR_PYTHON                   interpreter for the built-in notifier
#   ROBOTHOR_EXTRA_PATH               TEST-ONLY. Prepended to the fixed PATH
#                                     below so a test can point the drill at
#                                     its fakes. Nothing on the box sets it;
#                                     documented in docs/runbooks/RESTORE_DRILL.md.
set -uo pipefail

log() { echo "restore-drill: $*"; }
err() { echo "restore-drill: $*" >&2; }

# ── PATH ─────────────────────────────────────────────────────────────────────
# Built from scratch, and the inherited one is DISCARDED.
#
# This unit loads the same EnvironmentFile=/etc/robothor/robothor.env as every
# other one, and that file sets a PATH with NO /usr/sbin and NO /sbin. A tool
# the drill cannot find must not turn into "the backup did not restore" — that
# is a very different page from "psql is not installed", and only one of them
# is true.
#
# The inherited PATH is not merely extended, because it begins with a
# user-writable ~/.local/bin and this drill runs as root: anything dropped
# there that shadows date, find, gunzip, psql, createdb or dropdb would run as
# root, and dropdb is the one destructive verb in this script. Appending
# system directories does not help — the planted copy still wins. So the PATH
# is a fixed list, with /usr/local/bin on it because this instance's rclone
# lives there.
#
# ROBOTHOR_EXTRA_PATH is the one prepended element and it is test-only.
export PATH="${ROBOTHOR_EXTRA_PATH:+$ROBOTHOR_EXTRA_PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Checked immediately, because `readlink` and `dirname` run BEFORE the tool
# preflight below. REPO_ROOT is where the built-in notifier looks for the
# interpreter, so a SCRIPT_DIR that did not resolve means a drill that creates
# a scratch database, restores into it and then has nowhere to deliver the
# verdict. `backup-state.sh` is the sibling that proves this really is the
# scripts directory of a checkout and not whatever `dirname ""` returned.
if [[ -z "$SCRIPT_DIR" || ! -r "${SCRIPT_DIR}/backup-state.sh" ]]; then
    err "cannot read ${SCRIPT_DIR:-<unresolved>}/backup-state.sh — the drill could not locate its own checkout (readlink/dirname resolved to '${SCRIPT_DIR:-}'). Nothing was created and nothing was restored: this is a broken install, not a backup that failed to restore."
    exit 2
fi

DRILL_DB="${ROBOTHOR_RESTORE_DRILL_DB:-robothor_restore_drill}"
LIVE_DB="${ROBOTHOR_DB_NAME:-robothor_memory}"
REMOTE="${ROBOTHOR_OFFSITE_REMOTE:-}"
LOCAL_DIR="${ROBOTHOR_RESTORE_DRILL_LOCAL_DIR:-/mnt/robothor-backup/robothor/db}"
WORK_DIR="${ROBOTHOR_RESTORE_DRILL_WORK_DIR:-}"
RCLONE_CMD="${ROBOTHOR_RESTORE_DRILL_RCLONE_CMD:-rclone}"
PSQL="${ROBOTHOR_RESTORE_DRILL_PSQL:-psql}"
CREATEDB="${ROBOTHOR_RESTORE_DRILL_CREATEDB:-createdb}"
DROPDB="${ROBOTHOR_RESTORE_DRILL_DROPDB:-dropdb}"
DROP_TIMEOUT="${ROBOTHOR_RESTORE_DRILL_DROP_TIMEOUT:-300}"
NOTIFY_CMD="${ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD:-}"

FETCHED=""
ERROR_LOG=""
# A work dir this run created with mktemp -d is ours to delete; one the
# operator configured is not — it may be a real directory with other things in
# it, and a cleanup that cannot tell the difference is a cleanup nobody dares
# enable.
WORK_DIR_IS_OURS=0

# ── The result always reaches someone ────────────────────────────────────────
# Written as an `info` notification, which robothor/engine/alerts.py routes to
# an alert_digest row for main's heartbeat rather than a page. A drill result is
# news, not an emergency — and a drill whose result goes only to a journal
# nobody reads is the "quarterly by hand" arrangement with extra steps.
notify() {
    local subject="$1" body="$2"
    if [[ -n "$NOTIFY_CMD" ]]; then
        local argv
        read -r -a argv <<<"$NOTIFY_CMD"
        "${argv[@]}" "$subject" "$body" \
            || err "the notify command failed; the result is in this journal only"
        return 0
    fi
    local py="${ROBOTHOR_PYTHON:-${REPO_ROOT}/venv/bin/python}"
    if [[ ! -x "$py" ]]; then
        err "no interpreter at ${py} — the drill result is in this journal only"
        return 0
    fi
    "$py" - "$subject" "$body" <<'PY' \
        || err "could not write the drill notification; the result is in this journal only"
import sys

from robothor.crm.dal import send_notification

send_notification(
    from_agent="restore-drill",
    to_agent="main",
    notification_type="alert_digest",
    subject=f"[info] {sys.argv[1]}",
    body=sys.argv[2],
)
PY
}

abort() {
    err "$1"
    notify "Restore drill FAILED" "$1

Runbook: docs/runbooks/RESTORE_DRILL.md"
    exit 1
}

# ── dropdb, bounded ──────────────────────────────────────────────────────────
# `dropdb` blocks for as long as ANY backend is still connected to the target,
# and it waits forever. Two overlapping runs are enough to produce that: 27
# backends wedged on DROP DATABASE while the drill sat there. Unbounded, the
# unit then dies at TimeoutStartSec, which reads as "the drill never finished"
# — a very different page from "the cleanup could not complete", and only one
# of them is true.
#
# So the drop is capped and a blown cap is loud and non-zero (the unit's
# OnFailure= pages), never a hang. DROP_TIMED_OUT latches: the EXIT trap drops
# the same database, and retrying a drop already shown to hang just buys a
# second full budget on the way out.
DROP_TIMED_OUT=0
drop_scratch_db() {
    (( DROP_TIMED_OUT )) && return 1
    local rc=0
    timeout "$DROP_TIMEOUT" "$DROPDB" --if-exists "$DRILL_DB" >/dev/null 2>&1 || rc=$?
    # 124 is timeout's own verdict; 137 is the SIGKILL it escalates to.
    if (( rc == 124 || rc == 137 )); then
        DROP_TIMED_OUT=1
        err "dropdb on ${DRILL_DB} did not return within ${DROP_TIMEOUT}s — something still holds a connection to it. The scratch database is still there and has to be dropped by hand; see docs/runbooks/RESTORE_DRILL.md."
        return 1
    fi
    return 0
}

cleanup() {
    # The status the script was going to exit with, before any cleanup ran.
    local rc=$?

    # Every part is best-effort and every part matters: an orphan scratch
    # database fills the disk one month at a time, a fetched dump is a full
    # copy of the production data sitting in a work directory, and a monthly
    # unit that leaks one temp file per run leaks twelve a year — including
    # from the abort paths, which is where a broken drill spends its time.
    drop_scratch_db || rc=1
    [[ -n "$FETCHED" && -f "$FETCHED" ]] && rm -f "$FETCHED"
    [[ -n "$ERROR_LOG" && -f "$ERROR_LOG" ]] && rm -f "$ERROR_LOG"
    [[ "$WORK_DIR_IS_OURS" == 1 && -n "$WORK_DIR" && -d "$WORK_DIR" ]] && rm -rf "$WORK_DIR"
    exit "$rc"
}

# ── 1. Refuse anything but a scratch target ──────────────────────────────────
# Before a connection is opened, and before the EXIT trap that can call dropdb
# is installed.
case "$DRILL_DB" in
    "$LIVE_DB" | postgres | template0 | template1)
        err "refusing to run the drill against ${DRILL_DB} — that is a live or template database"
        exit 1
        ;;
esac
if [[ "$DRILL_DB" != *drill* ]]; then
    err "refusing to run the drill against ${DRILL_DB} — the scratch database name must contain 'drill', because this script ends by dropping it"
    exit 1
fi

# ── 1b. Resolve every external tool before creating anything ─────────────────
# A binary that is not on PATH has to say which binary, BEFORE a scratch
# database exists and a restore is being timed. Otherwise a missing psql is
# reported as a backup that would not restore — the one conclusion this drill
# exists to make trustworthy. Runs before the EXIT trap on purpose: nothing has
# been created yet, so there is nothing to clean up.
MISSING=()
require_tool() {
    local what="$1" cmd="$2" argv
    [[ -n "$cmd" ]] || return 0
    read -r -a argv <<<"$cmd"
    command -v "${argv[0]}" >/dev/null 2>&1 || MISSING+=("${what} — ${argv[0]}")
}

# `timeout` is load-bearing, not decoration: it is what keeps a wedged dropdb
# from running out the unit's TimeoutStartSec. `env` is what the notifier and
# the seams are invoked through.
for tool in date find sort head cut mktemp stat gunzip rm timeout env; do
    require_tool "core utility" "$tool"
done
require_tool "the restore (ROBOTHOR_RESTORE_DRILL_PSQL)" "$PSQL"
require_tool "the scratch database (ROBOTHOR_RESTORE_DRILL_CREATEDB)" "$CREATEDB"
require_tool "the cleanup (ROBOTHOR_RESTORE_DRILL_DROPDB)" "$DROPDB"
# Only when a remote is configured — a box with none never lists it.
[[ -z "$REMOTE" ]] || require_tool "the offsite fetch (ROBOTHOR_RESTORE_DRILL_RCLONE_CMD)" "$RCLONE_CMD"
[[ -z "$NOTIFY_CMD" ]] || require_tool "the notifier (ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD)" "$NOTIFY_CMD"

if (( ${#MISSING[@]} > 0 )); then
    for tool in "${MISSING[@]}"; do
        err "MISSING ${tool}"
    done
    abort "the drill cannot run: ${#MISSING[@]} tool(s) do not resolve on PATH=${PATH} (${MISSING[*]}). Nothing was created and nothing was restored — this is a misconfiguration, not a backup that failed to restore. Install the tool, or point its seam at one; the drill's PATH is fixed and does not inherit the caller's."
fi

# Installed here: after the name guards (so the dropdb in cleanup can only ever
# reach a validated scratch name) and BEFORE anything is created, so the work
# directory and the error log are covered on every abort path too.
trap cleanup EXIT

# ── 2. Find a dump: offsite first, local as the fallback ─────────────────────
DUMP=""
SOURCE=""

fetch_offsite() {
    local argv newest
    read -r -a argv <<<"$RCLONE_CMD"
    newest="$("${argv[@]}" lsf "${REMOTE}/db" --include '*.sql.gz' 2>/dev/null | sort | tail -n 1)"
    newest="${newest%/}"
    if [[ -z "$newest" ]]; then
        err "the offsite remote ${REMOTE}/db listed no dumps — falling back to the local copy"
        return 1
    fi
    mkdir -p "$WORK_DIR" 2>/dev/null || true
    if ! "${argv[@]}" copyto "${REMOTE}/db/${newest}" "${WORK_DIR}/${newest}" >/dev/null 2>&1; then
        err "could not fetch ${REMOTE}/db/${newest} — falling back to the local copy"
        return 1
    fi
    DUMP="${WORK_DIR}/${newest}"
    FETCHED="$DUMP"
    SOURCE="offsite ${REMOTE}/db"
    return 0
}

find_local() {
    local newest
    newest="$(find "$LOCAL_DIR" -maxdepth 1 -type f -name '*.sql.gz' -printf '%T@ %p\n' \
        2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2-)"
    [[ -n "$newest" ]] || return 1
    DUMP="$newest"
    SOURCE="local ${LOCAL_DIR}"
    return 0
}

if [[ -z "$WORK_DIR" ]]; then
    if WORK_DIR="$(mktemp -d 2>/dev/null)"; then
        WORK_DIR_IS_OURS=1
    else
        WORK_DIR=""
    fi
fi

if [[ -n "$REMOTE" ]]; then
    log "drilling from the offsite copy first — that is the path a box loss actually takes"
    fetch_offsite || true
else
    log "no ROBOTHOR_OFFSITE_REMOTE configured — drilling from the local copy"
fi

if [[ -z "$DUMP" ]]; then
    find_local || true
fi

# The 2026-08-24 guard. An empty dump variable must abort: the pipeline below
# would otherwise "succeed" in a fraction of a second against nothing at all,
# and record that as a passing drill.
[[ -n "$DUMP" ]] || abort "NO DUMP AVAILABLE — the offsite remote listed nothing and no *.sql.gz was found in ${LOCAL_DIR}. There is no restorable copy to drill; this is the condition the drill exists to detect, not a reason to skip it."

DUMP_NAME="$(basename "$DUMP")"
DUMP_BYTES="$(stat -c %s "$DUMP" 2>/dev/null || echo 0)"
log "drilling ${DUMP_NAME} (${DUMP_BYTES} bytes) from ${SOURCE}"

# ── 3. Timed restore into the scratch database ───────────────────────────────
if ! drop_scratch_db; then
    abort "the scratch database ${DRILL_DB} could not be dropped before the restore (dropdb did not return within ${DROP_TIMEOUT}s). Nothing was restored — this is a stuck cleanup, not a backup that failed to restore."
fi
if ! "$CREATEDB" "$DRILL_DB" 2>&1; then
    abort "could not create the scratch database ${DRILL_DB} — the drill could not run"
fi

ERROR_LOG="$(mktemp 2>/dev/null || echo /tmp/restore-drill-errors.log)"
START="$(date +%s)"
# ON_ERROR_STOP=0 deliberately: the runbook's baselines count errors rather
# than stopping at the first one, because a dump that restores 115 of 116
# tables is a materially different answer from one that restores none.
gunzip -c "$DUMP" 2>/dev/null | "$PSQL" -q -d "$DRILL_DB" -v ON_ERROR_STOP=0 >/dev/null 2>"$ERROR_LOG"
ELAPSED=$(( $(date +%s) - START ))

ERRORS="$(grep -c '^ERROR' "$ERROR_LOG" 2>/dev/null)" || ERRORS=0
[[ "$ERRORS" =~ ^[0-9]+$ ]] || ERRORS=0

# ── 4. Verify: ask the restored database what is in it ───────────────────────
# ANALYZE first — reltuples is -1 on a freshly restored table, and a row count
# of "-1" reported as evidence of a good restore would be worse than no check.
"$PSQL" -q -d "$DRILL_DB" -c "ANALYZE" >/dev/null 2>&1 || true

TABLES="$("$PSQL" -tAd "$DRILL_DB" -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" 2>/dev/null)"
[[ "$TABLES" =~ ^[0-9]+$ ]] || TABLES=0

ROWS="$("$PSQL" -tAd "$DRILL_DB" -c \
    "SELECT COALESCE(sum(c.reltuples)::bigint, 0) FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind = 'r' AND n.nspname = 'public'" 2>/dev/null)"
[[ "$ROWS" =~ ^-?[0-9]+$ ]] || ROWS=0

TOP="$("$PSQL" -tAd "$DRILL_DB" -c \
    "SELECT COALESCE(string_agg(relname || '=' || rows, ', '), '-') FROM (
       SELECT c.relname, c.reltuples::bigint AS rows FROM pg_class c
       JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE c.relkind = 'r' AND n.nspname = 'public'
       ORDER BY c.reltuples DESC LIMIT 3) t" 2>/dev/null)"
[[ -n "$TOP" ]] || TOP="-"

log "restored in ${ELAPSED}s: ${TABLES} tables, ~${ROWS} rows, ${ERRORS} errors"
log "largest tables: ${TOP}"

RESULT="dump:      ${DUMP_NAME} (${DUMP_BYTES} bytes)
source:    ${SOURCE}
duration:  ${ELAPSED}s
tables:    ${TABLES}
rows:      ~${ROWS}
errors:    ${ERRORS}
largest:   ${TOP}
target:    ${DRILL_DB} (created and dropped by this run)

Runbook and measured baselines: docs/runbooks/RESTORE_DRILL.md"

# Exit status is not evidence of a restore. psql exiting 0 says only that it
# read the file — the 2026-08-24 drill "passed" against an empty database.
if (( TABLES == 0 )); then
    abort "RESTORE PRODUCED NO TABLES — ${DUMP_NAME} read without error but reconstituted nothing. psql's exit status says only that it read the file.

${RESULT}"
fi

notify "Restore drill PASSED in ${ELAPSED}s" "$RESULT"
log "drill PASSED"
exit 0
