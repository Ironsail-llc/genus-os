#!/usr/bin/env bash
# The reliability dead-man: page on the AGE of the newest good backup, not on
# a unit's exit code.
#
# Run by robothor-slo.timer (hourly). Dependency-free on purpose: bash, date,
# find and the credentials handled by scripts/send_failure_alert.sh. It imports
# nothing from the engine, and it must keep working when the database, the
# engine and the backup volume are all gone — those are the conditions it
# exists to report.
#
# WHY A LEVEL-TRIGGERED PROBE EXISTS
#   Every unit in the backup chain pages via OnFailure=. On 2026-08-27 the
#   encrypted USB backup volume dropped off the bus and stayed off for two
#   days, and those units did page — ~22 Telegram messages whose entire
#   content was a unit name. None of them answered the only question an
#   operator has: how old is the newest restorable copy?
#
#   Then it got quieter. scripts/backup-volume-check.sh landed as
#   ExecCondition=, so a wedged volume now makes the backup units SKIP
#   (Result=exec-condition) — deliberately, to end a 96-page-a-day storm. A
#   skipped unit fires no OnFailure= at all. A timer that stops firing fails
#   nothing either. Both signals are EDGE-triggered: they can only speak when
#   a run happens.
#
#   This probe is LEVEL-triggered. It reads the age of the newest good backup
#   on its own timer and keeps paging while that age is out of budget. Fix the
#   volume and it goes silent by itself; ignore it and it comes back. That is
#   the whole difference between a pager and a dead-man.
#
# AN UNREADABLE DIRECTORY IS A BREACH, NEVER A SKIP
#   ext4 sets `emergency_ro` when the device disappears mid-write. stat() keeps
#   answering: the mountpoint is still a mountpoint, `[[ -d ]]` still passes,
#   `df` still reports the cached capacity. Only readdir() fails. Every guard
#   the backup chain had was a stat() guard, which is exactly why two days went
#   unnoticed. So "I could not read the dump directory" is reported as a
#   BREACH here — never as "no news".
#
#   And a fresh marker never vouches for an unreadable directory. The markers
#   live on NVMe (scripts/backup-state.sh, on purpose: the disk that breaks
#   must not hold the evidence of when it last worked), so the last stamp
#   before the drive fell off the bus stays fresh forever.
#
# DEDUP BELONGS TO THE SENDER
#   send_failure_alert.sh already dedups by key with a stamp file. This probe
#   calls it every hour while a breach stands and passes the per-SLO cooldown
#   in the environment; the sender turns that into a re-page, not a storm.
#   Dedup lives in exactly one place, as it does for liveness_probe.sh.
#
# Usage: slo_probe.sh            page for every breached SLO
#        slo_probe.sh --report   measure the DB-free SLOs, print one row each
#                                on stdout, page NOBODY, exit 0
#
# --report EXISTS SO THERE IS ONE IMPLEMENTATION
#   The daily surface (scripts/guardrail_watch.py) used to read the last-good
#   markers only, while this probe takes the WORSE of (marker, newest file) and
#   adds a readdir and a volume probe. The 2026-08-27 volume drop is exactly
#   the state those two answer differently: the markers live on NVMe and stay
#   fresh forever, so the daily report said OK while the pager said BREACH. The
#   daily report now runs this script instead of measuring it a second way.
#
#   Row format, tab-separated: SLO<TAB>name<TAB>target<TAB>measured<TAB>status
#   where status is OK | BREACH | UNEVALUATED.
#
# Exit: 0 every evaluated SLO is inside budget, or every breach was
#         successfully handed to the sender
#       1 a breach was found and its page could NOT be delivered, or an SLO
#         could NOT be evaluated at all — an inert dead-man must be loud, and
#         the unit's OnFailure= is the only voice an unevaluated check has
#       2 the probe is misconfigured and cannot answer
#
# Environment:
#   ROBOTHOR_SLO_LOCAL_DUMP_DIR        nightly dump dir
#                                      (/mnt/robothor-backup/robothor/db)
#   ROBOTHOR_BACKUP_STATE_DIR          last-good markers, read via
#                                      scripts/backup-state.sh
#   ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS  local dump budget (26)
#   ROBOTHOR_SLO_OFFSITE_MAX_HOURS     offsite budget (26)
#   ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS  base backup budget (192 = 8d)
#   ROBOTHOR_SLO_BASEBACKUP_DIR        base backups, for the marker-free
#   / ROBOTHOR_BASEBACKUP_DIR          fallback (same spelling and default as
#                                      scripts/pg-basebackup.sh)
#   ROBOTHOR_SLO_BACKUP_COOLDOWN_SECONDS    12h — hourly probe, so the
#                                      standing breach re-pages daily
#   ROBOTHOR_SLO_HEARTBEAT_COOLDOWN_SECONDS 12h
#   ROBOTHOR_SLO_LLM_COOLDOWN_SECONDS       6h
#   ROBOTHOR_SLO_VOLUME_CHECK_CMD      volume probe; the dump dir is appended
#                                      (scripts/backup-volume-check.sh --ro)
#   ROBOTHOR_SLO_SYSTEMCTL_CMD         systemctl, for S5 and S8
#   ROBOTHOR_SLO_GUARDRAIL_WATCH_MAX_HOURS  S8 budget (26)
#   ROBOTHOR_SLO_LIVENESS_MAX_HOURS         S5 budget (1)
#   ROBOTHOR_SLO_GUARDRAIL_COOLDOWN_SECONDS 12h
#   ROBOTHOR_SLO_LIVENESS_COOLDOWN_SECONDS  12h
#   ROBOTHOR_SLO_RCLONE_CMD            rclone, for the offsite listing
#   ROBOTHOR_OFFSITE_REMOTE            rclone remote (shared with backup-offsite.sh)
#   ROBOTHOR_SLO_ALERT_CMD             replaces the default sender
#   ROBOTHOR_SLO_PSQL_CMD              psql, for the two DB-backed SLOs.
#                                      Unset and running as root, the query
#                                      hops to an OS ACCOUNT with runuser:
#                                      pg_hba uses peer auth on the socket and
#                                      pg_ident maps that account onto the
#                                      role. Root is mapped to nothing.
#   PGUSER / ROBOTHOR_DB_USER          the database ROLE (the unit sets PGUSER)
#   ROBOTHOR_SLO_OS_USER               the OS ACCOUNT to hop to; defaults to
#                                      ROBOTHOR_SERVICE_USER. NOT the role —
#                                      `runuser -u <role>` fails with "user
#                                      does not exist" and measures nothing.
#   ROBOTHOR_SLO_RUNUSER_CMD           the hop itself (runuser)
#   ROBOTHOR_SLO_GETENT_CMD            getent, to prove the account exists
#   ROBOTHOR_SLO_DB / ROBOTHOR_DB_NAME database to query (robothor_memory)
#   ROBOTHOR_SLO_DB_CHECKS             0 disables the DB-backed SLOs
#   ROBOTHOR_SLO_HEARTBEAT_AGENT       operator-facing agent id (main)
#   ROBOTHOR_SLO_PROBE_TIMEOUT         seconds per disk step (20) — a dropped
#                                      USB device blocks readdir forever
set -uo pipefail

REPORT=0
if [[ "${1:-}" == "--report" ]]; then
    REPORT=1
    shift
fi
if (( $# > 0 )); then
    echo "slo_probe: usage: slo_probe.sh [--report]" >&2
    exit 2
fi

# In report mode stdout carries the machine-readable rows and nothing else, so
# the running commentary moves to stderr rather than disappearing.
log() {
    if (( REPORT )); then
        echo "slo_probe: $*" >&2
    else
        echo "slo_probe: $*"
    fi
}
err() { echo "slo_probe: $*" >&2; }

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# shellcheck source=scripts/backup-state.sh
source "${SCRIPT_DIR}/backup-state.sh"

DUMP_DIR="${ROBOTHOR_SLO_LOCAL_DUMP_DIR:-/mnt/robothor-backup/robothor/db}"
LOCAL_MAX_HOURS="${ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS:-26}"
OFFSITE_MAX_HOURS="${ROBOTHOR_SLO_OFFSITE_MAX_HOURS:-26}"
BASEBACKUP_MAX_HOURS="${ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS:-192}"
# Same spelling and same default as scripts/pg-basebackup.sh and wal-offsite.sh,
# so the probe reads the directory those two actually write.
BASEBACKUP_DIR="${ROBOTHOR_SLO_BASEBACKUP_DIR:-${ROBOTHOR_BASEBACKUP_DIR:-/mnt/robothor-backup/robothor/basebackup}}"
# S8: the daily report runs at 08:30, so 26h is one missed run plus slack.
GUARDRAIL_WATCH_MAX_HOURS="${ROBOTHOR_SLO_GUARDRAIL_WATCH_MAX_HOURS:-26}"
# S5: the liveness timer fires every 5 minutes. An hour is twelve missed ticks.
LIVENESS_MAX_HOURS="${ROBOTHOR_SLO_LIVENESS_MAX_HOURS:-1}"

BACKUP_COOLDOWN="${ROBOTHOR_SLO_BACKUP_COOLDOWN_SECONDS:-43200}"
HEARTBEAT_COOLDOWN="${ROBOTHOR_SLO_HEARTBEAT_COOLDOWN_SECONDS:-43200}"
LLM_COOLDOWN="${ROBOTHOR_SLO_LLM_COOLDOWN_SECONDS:-21600}"
GUARDRAIL_COOLDOWN="${ROBOTHOR_SLO_GUARDRAIL_COOLDOWN_SECONDS:-43200}"
LIVENESS_COOLDOWN="${ROBOTHOR_SLO_LIVENESS_COOLDOWN_SECONDS:-43200}"

SYSTEMCTL_CMD="${ROBOTHOR_SLO_SYSTEMCTL_CMD:-systemctl}"
VOLUME_CHECK_CMD="${ROBOTHOR_SLO_VOLUME_CHECK_CMD:-/usr/bin/env bash ${SCRIPT_DIR}/backup-volume-check.sh --ro}"
RCLONE_CMD="${ROBOTHOR_SLO_RCLONE_CMD:-rclone}"
REMOTE="${ROBOTHOR_OFFSITE_REMOTE:-}"
ALERT_CMD="${ROBOTHOR_SLO_ALERT_CMD:-/usr/bin/env bash ${SCRIPT_DIR}/send_failure_alert.sh}"

# pg_hba.conf uses peer auth on the Unix socket and pg_ident maps an OS
# ACCOUNT onto a database ROLE. Those are two different names: the role
# typically has no passwd entry at all, while the OS accounts pg_ident maps
# onto it are the service user and `postgres`.
#
# So the hop takes the OS account and carries the role across in PGUSER.
# Handing the role to `runuser -u` instead fails with "user <role> does not
# exist" on EVERY run: S2 and S6 stay UNEVALUATED while the unit exits
# non-zero, so its OnFailure= pages hourly and measures nothing. A pager that
# only ever cries wolf gets muted, and then the real breach is silent too.
#
# This unit deliberately runs as root (the pager recovers the secrets with the
# root-readable age key) and root is not in pg_ident's map, so with no OS
# account to become there is no query to run — reported as UNEVALUATED naming
# the account, never as silence.
DB_ROLE="${PGUSER:-${ROBOTHOR_DB_USER:-}}"
OS_USER="${ROBOTHOR_SLO_OS_USER:-${ROBOTHOR_SERVICE_USER:-}}"
RUNUSER_CMD="${ROBOTHOR_SLO_RUNUSER_CMD:-runuser}"
GETENT_CMD="${ROBOTHOR_SLO_GETENT_CMD:-getent}"
PSQL_CMD="${ROBOTHOR_SLO_PSQL_CMD:-}"
DB="${ROBOTHOR_SLO_DB:-${PGDATABASE:-${ROBOTHOR_DB_NAME:-robothor_memory}}}"
# Why S2/S6 could not be measured at all, in the words the operator needs.
DB_BLOCKED=""
DB_CHECKS="${ROBOTHOR_SLO_DB_CHECKS:-1}"
HEARTBEAT_AGENT="${ROBOTHOR_SLO_HEARTBEAT_AGENT:-main}"

PROBE_TIMEOUT="${ROBOTHOR_SLO_PROBE_TIMEOUT:-20}"
if [[ ! "$PROBE_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    err "ROBOTHOR_SLO_PROBE_TIMEOUT=${PROBE_TIMEOUT} is not a positive integer"
    exit 2
fi

NOW="$(date +%s)"
UNDELIVERED=0
UNEVALUATED=0

# One row per SLO the DB-free half evaluated, for --report. Collected in both
# modes: a row that only exists in report mode is a second implementation
# again, and would drift the same way.
ROWS=()
emit() { ROWS+=("$(printf 'SLO\t%s\t%s\t%s\t%s' "$1" "$2" "$3" "$4")"); }

# ── Paging ───────────────────────────────────────────────────────────────────
# One page per SLO, keyed `slo:<name>` rather than by a systemd unit. A
# unit-keyed stamp would let an unrelated unit's page mute this one — the
# sender's cooldown file is keyed on exactly the string it is handed.
#
# The cooldown is passed per call, not set once for the whole unit: the backup
# dead-man wants 12h (re-page daily until fixed) and the LLM-availability SLO
# wants 6h, and a single Environment= line in the unit cannot express both.
page() {
    local key="$1" cooldown="$2" body="$3"
    local argv
    if (( REPORT )); then
        # --report measures; it never interrupts anyone. The daily surface
        # calling this must not double every page the hourly timer sends.
        log "would page ${key} (report mode: not sending)"
        return 0
    fi
    read -r -a argv <<<"$ALERT_CMD"
    if ROBOTHOR_ALERT_COOLDOWN_SECONDS="$cooldown" "${argv[@]}" "$key" "$body"; then
        log "page for ${key} handed to the sender successfully"
        return 0
    fi
    # Checked, not assumed — `delivered = bool(sent)`, per
    # robothor/engine/alerts.py, where assuming the send worked hid an arity
    # bug while 432+ alerts went nowhere. The sender exits 0 both on a
    # delivered page and on one it suppressed inside its cooldown, so a
    # non-zero status here means the page genuinely did not land.
    err "page for ${key} was NOT delivered — the sender failed"
    UNDELIVERED=1
    return 1
}

# ── Age helpers ──────────────────────────────────────────────────────────────

# Epoch seconds of a last-good marker, or non-zero when no run was ever
# recorded. An absent marker reads as "recent" to anything that only checks for
# a non-empty string; it means the opposite.
marker_epoch() {
    local ts
    ts="$(backup_state_last_ts "$1")" || return 1
    date -d "$ts" +%s 2>/dev/null || return 1
}

hours_since() { echo $(( (NOW - $1) / 3600 )); }

# ── S4: backup freshness (the dead-man) ──────────────────────────────────────

BACKUP_BREACHES=()
breach() { BACKUP_BREACHES+=("$1"); err "BREACH: $1"; }

# 1. Is the volume usable at all? backup-volume-check.sh answers 1 for "not
#    usable" (which makes the backup units skip quietly) and 255 for "this
#    probe cannot answer". Here both are loud: this is the half of that
#    arrangement that is allowed to shout.
check_volume() {
    local argv rc
    read -r -a argv <<<"$VOLUME_CHECK_CMD"
    "${argv[@]}" "$DUMP_DIR" >/dev/null 2>&1
    rc=$?
    local label="S4 backup freshness: volume"
    case "$rc" in
        0)
            log "backup volume at ${DUMP_DIR}: healthy"
            emit "$label" "usable" "healthy" "OK"
            ;;
        255)
            breach "the backup volume probe could not run against ${DUMP_DIR} (exit 255) — volume health is UNKNOWN"
            emit "$label" "usable" "UNKNOWN — the volume probe could not run" "BREACH"
            ;;
        *)
            breach "the backup volume at ${DUMP_DIR} is NOT usable (volume check exit ${rc}) — the backup units are being skipped, not failing"
            emit "$label" "usable" "not usable (volume check exit ${rc})" "BREACH"
            ;;
    esac
}

# 2. The local dump tier. The readdir is unconditional and comes FIRST: it is
#    the one syscall emergency_ro actually breaks, and a marker on NVMe cannot
#    speak for a directory on a disk that is gone.
check_local_dump() {
    local readable=1 file_epoch="" mark_epoch="" newest="" age
    local label="S4 backup freshness: local dump" target="< ${LOCAL_MAX_HOURS}h"
    if ! timeout "$PROBE_TIMEOUT" ls -A "$DUMP_DIR" >/dev/null 2>&1; then
        readable=0
        breach "cannot read the local dump directory ${DUMP_DIR} (readdir failed or timed out after ${PROBE_TIMEOUT}s) — stat() still answers on a dropped USB device, so this is the ONLY signal that the volume is gone"
    fi

    if (( readable )); then
        newest="$(timeout "$PROBE_TIMEOUT" find "$DUMP_DIR" -maxdepth 1 -type f \
            -name '*.sql.gz' -printf '%T@\n' 2>/dev/null | sort -rn | head -n 1)"
        [[ -n "$newest" ]] && file_epoch="${newest%%.*}"
    fi

    mark_epoch="$(marker_epoch last-local-dump)" || mark_epoch=""

    if [[ -z "$file_epoch" && -z "$mark_epoch" ]]; then
        if (( readable )); then
            breach "no *.sql.gz in ${DUMP_DIR} and no last-local-dump marker — there is NO restorable nightly dump"
            emit "$label" "$target" "no dump and no marker — nothing restorable" "BREACH"
        else
            emit "$label" "$target" "the dump directory could not be read" "BREACH"
        fi
        return
    fi

    # The WORSE of the two known ages. A marker with no file behind it and a
    # file with no marker are both real states, and taking the older answer is
    # the conservative reading a dead-man owes the operator.
    age="$file_epoch"
    if [[ -z "$age" ]] || { [[ -n "$mark_epoch" ]] && (( mark_epoch < age )); }; then
        age="$mark_epoch"
    fi
    age="$(hours_since "$age")"
    if (( age > LOCAL_MAX_HOURS )); then
        breach "newest local dump is ${age}h old (budget ${LOCAL_MAX_HOURS}h) — at least one nightly dump did not happen"
        emit "$label" "$target" "${age}h" "BREACH"
    elif (( readable )); then
        log "local dump: ${age}h old (budget ${LOCAL_MAX_HOURS}h) — OK"
        emit "$label" "$target" "${age}h" "OK"
    else
        emit "$label" "$target" "marker reads ${age}h, but the directory could not be read" "BREACH"
        # The breach above already stands. Saying "OK" on the next line would
        # be the marker vouching for a directory nobody can read — the exact
        # inversion this probe exists to prevent, printed in the operator's own
        # journal.
        log "local dump: the last-good marker reads ${age}h, but it lives on NVMe and cannot speak for an unreadable volume — NOT OK"
    fi
}

# 3. The offsite tier. Marker first (it records a run that actually replicated
#    and verified); the remote listing is the fallback for a box whose marker
#    dir was lost.
check_offsite() {
    local epoch="" listing age
    local label="S4 backup freshness: offsite" target="< ${OFFSITE_MAX_HOURS}h"
    epoch="$(marker_epoch last-offsite-ok)" || epoch=""

    if [[ -z "$epoch" && -n "$REMOTE" ]]; then
        local argv
        read -r -a argv <<<"$RCLONE_CMD"
        listing="$("${argv[@]}" lsf "${REMOTE}/db" --include '*.sql.gz' --format tp \
            2>/dev/null | sort -r | head -n 1)"
        if [[ -n "$listing" ]]; then
            epoch="$(date -d "${listing%%;*}" +%s 2>/dev/null || true)"
        fi
    fi

    if [[ -z "$epoch" ]]; then
        breach "offsite copy freshness is UNKNOWN — no successful offsite run recorded and the remote could not be listed; a box loss may have NO recoverable copy"
        emit "$label" "$target" "unknown — no successful run recorded" "BREACH"
        return
    fi

    age="$(hours_since "$epoch")"
    if (( age > OFFSITE_MAX_HOURS )); then
        breach "newest offsite copy is ${age}h old (budget ${OFFSITE_MAX_HOURS}h) — a box loss restores from that generation"
        emit "$label" "$target" "${age}h" "BREACH"
    else
        log "offsite: ${age}h old (budget ${OFFSITE_MAX_HOURS}h) — OK"
        emit "$label" "$target" "${age}h" "OK"
    fi
}

# 4. The base backup tier — weekly, so it carries its own much wider budget.
#    PITR must replay every WAL segment since this point, so a stale base
#    backup grows the restore TIME rather than losing data.
#
#    The marker is evidence that a run happened; the base-* directory is the
#    thing PITR actually starts FROM. They live on different disks — markers on
#    NVMe, backups on the volume — so either can outlive the other, and reading
#    only the marker made a missing one page "PITR has no starting point" while
#    a week-old base backup sat on the volume. A dead-man that cries about a
#    backup it is standing on gets muted like any other.
#
#    Newest mtime of a base-* DIRECTORY, never a file: pg-basebackup.sh writes
#    `base-<stamp>.backup_label` beside `base-<stamp>/`, and a few hundred bytes
#    of WAL position is not a restorable copy.
basebackup_artifact_epoch() {
    local argv newest
    # The volume probe first, exactly as the local dump tier does it: stat()
    # keeps answering on a dropped USB device, so a find that returns nothing
    # is indistinguishable from a volume that is gone until something asks.
    read -r -a argv <<<"$VOLUME_CHECK_CMD"
    "${argv[@]}" "$BASEBACKUP_DIR" >/dev/null 2>&1 || return 1
    newest="$(timeout "$PROBE_TIMEOUT" find "$BASEBACKUP_DIR" -mindepth 1 -maxdepth 1 \
        -type d -name 'base-*' -printf '%T@\n' 2>/dev/null | sort -rn | head -n 1)"
    [[ -n "$newest" ]] || return 1
    printf '%s' "${newest%%.*}"
}

check_basebackup() {
    local epoch="" age from="the last-basebackup marker" prefix=""
    local label="S4 backup freshness: basebackup" target="< ${BASEBACKUP_MAX_HOURS}h"
    epoch="$(marker_epoch last-basebackup)" || epoch=""

    if [[ -z "$epoch" ]]; then
        if epoch="$(basebackup_artifact_epoch)"; then
            from="the newest base-* directory in ${BASEBACKUP_DIR}"
            prefix="marker absent; "
        else
            epoch=""
        fi
    fi

    if [[ -z "$epoch" ]]; then
        breach "basebackup freshness is UNKNOWN — no successful base backup has ever been recorded and no base-* directory was found in ${BASEBACKUP_DIR}; PITR has no starting point"
        emit "$label" "$target" "unknown — no run recorded and no base-* on the volume" "BREACH"
        return
    fi

    age="$(hours_since "$epoch")"
    if (( age > BASEBACKUP_MAX_HOURS )); then
        breach "${prefix}newest basebackup is ${age}h old per ${from} (budget ${BASEBACKUP_MAX_HOURS}h) — PITR must replay every WAL segment since then, and the restore time grows nightly"
        emit "$label" "$target" "${prefix}${age}h" "BREACH"
    elif [[ -n "$prefix" ]]; then
        log "basebackup: marker absent; newest base-* directory is ${age}h old (budget ${BASEBACKUP_MAX_HOURS}h) — OK"
        emit "$label" "$target" "marker absent; newest base-* directory is ${age}h old" "OK"
    else
        log "basebackup: ${age}h old (budget ${BASEBACKUP_MAX_HOURS}h) — OK"
        emit "$label" "$target" "${age}h" "OK"
    fi
}

log "=== S4 backup freshness (dead-man) ==="
check_volume
check_local_dump
check_offsite
check_basebackup

if (( ${#BACKUP_BREACHES[@]} > 0 )); then
    # One page carrying EVERY breached tier. Three separate pages for one dead
    # volume is how a pager gets muted; one page that names all three is how an
    # operator knows whether anything restorable is left.
    body="Backup freshness SLO BREACHED (${#BACKUP_BREACHES[@]}):"
    for line in "${BACKUP_BREACHES[@]}"; do
        body+="
  - ${line}"
    done
    body+="

Budgets: local dump ${LOCAL_MAX_HOURS}h / offsite ${OFFSITE_MAX_HOURS}h / basebackup ${BASEBACKUP_MAX_HOURS}h.
Runbook: docs/runbooks/SLOS.md (S4). This re-pages until the age is back inside budget."
    page "slo:backup-freshness" "$BACKUP_COOLDOWN" "$body" || true
else
    log "S4 backup freshness: OK — every tier is inside budget"
fi

# ── S5 / S8: is the rest of the watchdog fleet still running? ────────────────
# Both of these used to be the string "OK" in the daily report — S8's evidence
# was the report printing itself, which says nothing at all on the day the
# report does not run, and S5 asserted that a timer exists rather than that it
# fired. systemd already knows both answers; ask it.

# `systemctl show <unit> -p A,B` prints one KEY=VALUE line per property, and an
# empty value for anything it has no answer for. An empty value is never read
# as fresh here: it means the unit has no completed run, which is the state
# this pair exists to find.
show_props() {
    local argv
    read -r -a argv <<<"$SYSTEMCTL_CMD"
    "${argv[@]}" show "$1" -p "$2" 2>/dev/null
}

prop_of() { grep -m 1 "^${2}=" <<<"$1" | cut -d= -f2- ; }

# systemd spells "never" three ways depending on the property: empty, `n/a`
# and `0`. All three are the absence of a run, not a timestamp.
stamp_epoch() {
    local stamp="$1"
    [[ -n "$stamp" && "$stamp" != "n/a" && "$stamp" != "0" ]] || return 1
    date -d "$stamp" +%s 2>/dev/null || return 1
}

unevaluated() {
    err "  $1 UNEVALUATED — $2"
    UNEVALUATED=1
}

# The Results that mean the run did not FINISH. Everything else that has an
# exit timestamp completed, whatever status it completed with.
#
# robothor-guardrail-watch.service is a Type=oneshot that exits 1 BY DESIGN
# whenever it has findings (a drifted drop-in, an invalid manifest, a guardrail
# whose effective mode is not the one its manifest records — guardrail_watch.py
# main()). That exit is the unit's own OnFailure= pager firing, and it has
# already reached the operator by the time this looks. Reading it as
# `Result != success` made S8 page "the daily report is failing, so the drift
# checks are not reaching anyone" on exactly the mornings they did reach
# someone: two pages for one event, and the second one false.
#
# S8 asks whether the report RAN, recently. Not whether it liked what it found.
INCOMPLETE_RESULTS="timeout signal core-dump watchdog"

check_guardrail_watch() {
    local out result status stamp epoch age how
    local label="S8 guardrail-watch ran" target="completed < ${GUARDRAIL_WATCH_MAX_HOURS}h ago"
    out="$(show_props robothor-guardrail-watch.service ExecMainExitTimestamp,ExecMainStatus,Result)"
    if [[ -z "$out" ]]; then
        unevaluated "S8" "systemctl could not answer for robothor-guardrail-watch.service"
        emit "$label" "$target" "systemctl could not answer" "UNEVALUATED"
        return
    fi
    result="$(prop_of "$out" Result)"
    status="$(prop_of "$out" ExecMainStatus)"
    stamp="$(prop_of "$out" ExecMainExitTimestamp)"

    if ! epoch="$(stamp_epoch "$stamp")"; then
        emit "$label" "$target" "no completed run on this box" "BREACH"
        page "slo:guardrail-watch-stale" "$GUARDRAIL_COOLDOWN" \
            "S8 BREACHED: robothor-guardrail-watch.service has no completed run on this box. The daily SLO report, the drop-in/host-script drift checks and instance manifest validation have never produced a result here. Runbook: docs/runbooks/SLOS.md (S8)." || true
        return
    fi
    age="$(hours_since "$epoch")"
    how="Result=${result:-<none>}, status ${status:-<none>}"

    if [[ " ${INCOMPLETE_RESULTS} " == *" ${result} "* ]]; then
        emit "$label" "$target" "last run ${age}h ago did not finish (${how})" "BREACH"
        page "slo:guardrail-watch-stale" "$GUARDRAIL_COOLDOWN" \
            "S8 BREACHED: robothor-guardrail-watch.service stopped mid-run ${age}h ago (Result=${result}). It did not finish, so nobody knows which half of the drift checks, manifest validation and SLO report ran — and a run that never reached its end fires no findings page of its own. Runbook: docs/runbooks/SLOS.md (S8)." || true
    elif (( age > GUARDRAIL_WATCH_MAX_HOURS )); then
        emit "$label" "$target" "last completed ${age}h ago" "BREACH"
        page "slo:guardrail-watch-stale" "$GUARDRAIL_COOLDOWN" \
            "S8 BREACHED: robothor-guardrail-watch.service last completed ${age}h ago (budget ${GUARDRAIL_WATCH_MAX_HOURS}h). A daily watchdog that stops running reports nothing and fails nothing — this is the only signal left. Runbook: docs/runbooks/SLOS.md (S8)." || true
    elif [[ "$result" == "success" ]]; then
        log "  S8 guardrail-watch: last completed ${age}h ago, ${how} (budget ${GUARDRAIL_WATCH_MAX_HOURS}h) — OK"
        emit "$label" "$target" "last completed ${age}h ago, ${how}" "OK"
    elif [[ "$status" == "1" ]]; then
        # The by-design exit: the report finished and had findings. Its own
        # OnFailure= has already paged them; S8 saying so again would be the
        # second, wrong page for one event.
        log "  S8 guardrail-watch: completed ${age}h ago and reported findings (${how}) — OK for S8; the findings paged on their own"
        emit "$label" "$target" "completed ${age}h ago and reported findings (${how})" "OK"
    else
        # Finished, but with a status the report has no vocabulary for — it
        # exits 0 or 1 and nothing else. Something else killed it after it
        # started writing.
        emit "$label" "$target" "last run ${age}h ago exited unexpectedly (${how})" "BREACH"
        page "slo:guardrail-watch-stale" "$GUARDRAIL_COOLDOWN" \
            "S8 BREACHED: robothor-guardrail-watch.service finished ${age}h ago with an unexpected exit (${how}). It exits 0 for a clean report and 1 for one with findings; anything else means it died partway, so the drift checks and manifest validation it carries may not have run at all. Runbook: docs/runbooks/SLOS.md (S8)." || true
    fi
}

check_liveness() {
    local out tout result stamp epoch age
    local label="S5 liveness" target="timer fired < ${LIVENESS_MAX_HOURS}h ago, Result=success"
    out="$(show_props robothor-liveness.service Result)"
    tout="$(show_props robothor-liveness.timer LastTriggerUSec)"
    if [[ -z "$out" || -z "$tout" ]]; then
        unevaluated "S5" "systemctl could not answer for the robothor-liveness units"
        emit "$label" "$target" "systemctl could not answer" "UNEVALUATED"
        return
    fi
    result="$(prop_of "$out" Result)"
    stamp="$(prop_of "$tout" LastTriggerUSec)"

    if ! epoch="$(stamp_epoch "$stamp")"; then
        emit "$label" "$target" "the timer has never fired" "BREACH"
        page "slo:liveness-stale" "$LIVENESS_COOLDOWN" \
            "S5 BREACHED: robothor-liveness.timer has never fired on this box. The engine watchdog that survives a hard kill is not watching. Runbook: docs/runbooks/SLOS.md (S5)." || true
        return
    fi
    age="$(hours_since "$epoch")"

    if [[ "$result" != "success" ]]; then
        emit "$label" "$target" "last run Result=${result}, timer fired ${age}h ago" "BREACH"
        page "slo:liveness-stale" "$LIVENESS_COOLDOWN" \
            "S5 BREACHED: robothor-liveness.service last ran with Result=${result} (timer last fired ${age}h ago). The watchdog itself is failing, so a dead engine would page nobody. Runbook: docs/runbooks/SLOS.md (S5)." || true
    elif (( age > LIVENESS_MAX_HOURS )); then
        emit "$label" "$target" "timer last fired ${age}h ago" "BREACH"
        page "slo:liveness-stale" "$LIVENESS_COOLDOWN" \
            "S5 BREACHED: robothor-liveness.timer last fired ${age}h ago (budget ${LIVENESS_MAX_HOURS}h) — it runs every 5 minutes. A timer that stops firing fails nothing, so nothing else would say this. Runbook: docs/runbooks/SLOS.md (S5)." || true
    else
        log "  S5 liveness: timer fired ${age}h ago, last run Result=${result} — OK"
        emit "$label" "$target" "timer fired ${age}h ago, Result=${result}" "OK"
    fi
}

log "=== S5 liveness / S8 guardrail-watch freshness ==="
check_liveness
check_guardrail_watch

if (( REPORT )); then
    # Everything above is DB-free, which is the whole contract of --report: the
    # daily surface has its own database section and must not pay for a second
    # connection here, and a database outage is exactly when the backup age
    # matters most. Rows out, exit 0 — a report is not a verdict.
    printf '%s\n' ${ROWS[@]+"${ROWS[@]}"}
    exit 0
fi

# ── S2 / S6: the DB-backed SLOs ──────────────────────────────────────────────
# Read-only, and deliberately last: a database outage must never stop the
# backup dead-man above from running, which is the ordering discipline
# guardrail_watch.py's main() learned on 2026-08-16.

# Resolved here rather than at startup, because failing to resolve IS a
# measurement result: it has to be reported as UNEVALUATED naming the account,
# and a command substitution cannot report one — it runs in a subshell.
resolve_psql() {
    [[ -z "$PSQL_CMD" ]] || return 0    # an explicit override answers for itself

    if [[ "$(id -u)" != "0" ]]; then
        # Not root: peer auth judges whatever account this already is.
        PSQL_CMD="psql"
        return 0
    fi
    if [[ -z "$OS_USER" ]]; then
        DB_BLOCKED="the probe runs as root, which pg_ident does not map to any database role, and no OS account was configured to hop to — set ROBOTHOR_SLO_OS_USER (or ROBOTHOR_SERVICE_USER) to the account pg_ident maps onto role '${DB_ROLE:-<unset>}'"
        return 1
    fi
    if [[ "$(id -un)" == "$OS_USER" ]]; then
        PSQL_CMD="psql"
        return 0
    fi

    # getent, not `id <name>`: the whole failure being closed here is a name
    # that is a database role and not an account, and only the passwd database
    # can tell those apart.
    local getent_argv
    read -r -a getent_argv <<<"$GETENT_CMD"
    if ! "${getent_argv[@]}" passwd "$OS_USER" >/dev/null 2>&1; then
        DB_BLOCKED="the OS account '${OS_USER}' does not exist (no passwd entry), so the probe cannot hop to it — ROBOTHOR_DB_USER is a database ROLE, not an OS user; set ROBOTHOR_SLO_OS_USER to the account pg_ident maps onto role '${DB_ROLE:-<unset>}'"
        return 1
    fi

    PSQL_CMD="${RUNUSER_CMD} -u ${OS_USER} -- env"
    [[ -z "$DB_ROLE" ]] || PSQL_CMD+=" PGUSER=${DB_ROLE}"
    PSQL_CMD+=" PGDATABASE=${DB} psql"
    return 0
}

# The identity a failed query ran under. "The database did not answer" and
# "the hop was refused" look identical from here, and an operator cannot tell
# them apart without knowing which account asked.
db_identity() {
    if [[ -n "${ROBOTHOR_SLO_PSQL_CMD:-}" ]]; then
        printf 'psql command overridden by ROBOTHOR_SLO_PSQL_CMD'
    elif [[ "$PSQL_CMD" == "psql" ]]; then
        printf 'as OS user %s, role %s, database %s' \
            "$(id -un)" "${DB_ROLE:-<unset>}" "$DB"
    else
        printf 'hopped to OS user %s with PGUSER=%s, database %s' \
            "$OS_USER" "${DB_ROLE:-<unset>}" "$DB"
    fi
}

db_query() {
    local argv out
    read -r -a argv <<<"$PSQL_CMD"
    out="$("${argv[@]}" -d "$DB" -tAc "$1" 2>/dev/null)" || return 1
    out="${out//[[:space:]]/}"
    [[ "$out" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$out"
}

check_db_slos() {
    log "=== S2 heartbeat delivery / S6 LLM availability ==="
    if [[ "$DB_CHECKS" == "0" ]]; then
        # Loud, and on stderr, because this switch retires half the dead-man
        # in silence: S2 and S6 stop being measured, nothing pages, and that
        # is indistinguishable from two SLOs that are permanently fine. It
        # exists for tests/test_slo_probe.py, which must never query the live
        # database. A parenthetical in the journal is how a mute like this
        # survives for months.
        err "!! ROBOTHOR_SLO_DB_CHECKS=0 — S2 (heartbeat delivery) and S6 (LLM availability) are NOT being measured. This mute is for tests only and must NEVER be set in production; see docs/runbooks/SLOS.md."
        return
    fi

    # No identity peer auth accepts means no query at all — for BOTH SLOs.
    # Reported, not swallowed: a dead-man that cannot reach its instrument is
    # exactly as blind as one nobody wired up.
    if ! resolve_psql; then
        unevaluated "S2" "$DB_BLOCKED"
        unevaluated "S6" "$DB_BLOCKED"
        return
    fi
    local how
    how="$(db_identity)"

    local beats failures
    if beats="$(db_query "SELECT count(*) FROM agent_runs
        WHERE started_at >= now() - interval '24 hours'
          AND agent_id = '${HEARTBEAT_AGENT}'
          AND trigger_detail LIKE 'heartbeat:%'")"; then
        if (( beats == 0 )); then
            page "slo:heartbeat-delivery" "$HEARTBEAT_COOLDOWN" \
                "S2 BREACHED: 0 ${HEARTBEAT_AGENT} heartbeat runs in the last 24h. The operator-facing agent has not run at all — no briefing, no digest, no delivery. Runbook: docs/runbooks/SLOS.md (S2)." || true
        else
            log "  S2 heartbeat runs in 24h: ${beats} — OK"
        fi
    else
        # Named, not swallowed. An SLO nobody can evaluate is not a passing
        # SLO, and a check that only ever reports "skipped" is indistinguishable
        # from one that cannot fire.
        unevaluated "S2" "the heartbeat query did not answer (${how}) — the database is unreachable, or the hop was refused"
    fi

    if failures="$(db_query "SELECT count(*) FROM agent_runs
        WHERE started_at >= now() - interval '1 hour'
          AND error_message ILIKE '%All models failed%'")"; then
        if (( failures >= 5 )); then
            page "slo:llm-availability" "$LLM_COOLDOWN" \
                "S6 BREACHED: ${failures} runs in the last hour ended with 'All models failed'. Every model shares one credential pool — check the key pool before assuming a provider outage. Runbook: docs/runbooks/SLOS.md (S6)." || true
        else
            log "  S6 'All models failed' in the last hour: ${failures} — OK"
        fi
    else
        unevaluated "S6" "the model-failure query did not answer (${how}) — the database is unreachable, or the hop was refused"
    fi
}

check_db_slos

if (( UNDELIVERED )); then
    err "at least one SLO page was NOT delivered — failing the unit so its own OnFailure= pages"
    exit 1
fi
if (( UNEVALUATED )); then
    # An SLO nobody could evaluate pages nobody by design — it is not a breach.
    # Exiting 0 here would make that silence indistinguishable from health,
    # which is how six built-and-wired controls turned out to be inert. The
    # unit's OnFailure= is the only voice an unevaluated check has.
    err "at least one SLO could NOT be evaluated — failing the unit so its own OnFailure= pages"
    exit 1
fi
exit 0
