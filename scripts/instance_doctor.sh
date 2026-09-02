#!/usr/bin/env bash
# Install truth: what is on this box that the repo did not put there?
#
# scripts/install-units.sh reports installed / updated / unchanged. That is one
# direction only — it can say what the repo pushed onto the box, and never what
# is on the box that no template describes. Everything below was live on the
# first Genus OS instance with no command anywhere that would print it: a timer
# SYMLINKED into the repo checkout (never rendered, and a checkout move would
# have unscheduled it silently), nine live robothor-* units with no template at
# all (two of them active), twelve `.bak-*` files in a drop-in directory that
# systemd ignores but a human reading the directory does not, hand-written
# drop-ins with no repo mirror, a service enabled but not running, and flags set
# in BOTH /etc/robothor/robothor.env and a drop-in's Environment= where the env
# file silently wins.
#
# READ-ONLY by design: it runs from a timer on a live box and changes nothing.
# Every comparison is delegated to the scripts that already own it —
# scripts/render-unit.sh (via scripts/check_dropin_drift.sh) renders templates
# before diffing, so a placeholder-bearing mirror is not mistaken for drift.
#
# Usage: instance_doctor.sh [--root DIR] [--allow-file FILE] [--env-file FILE]
#   --root DIR        filesystem root to inspect (default /, so units are read
#                     from /etc/systemd/system; override for tests)
#   --allow-file FILE units and drop-ins that are deliberately instance-only and
#                     must not be reported as untemplated. One name per line;
#                     `#` comments and blank lines ignored. Entries are either a
#                     unit file name (robothor-x.service) or a drop-in path
#                     (robothor-x.service.d/y.conf). Default:
#                     <root>/etc/robothor/instance-units.allow — instance-land,
#                     next to robothor.env, deliberately NOT in the repo.
#                     It suppresses ONE finding class: no-template /
#                     unmirrored-dropin. It is not a mute button — drift, inert
#                     files, symlinked units, enabled≠active and env shadowing
#                     are still reported for a unit named here, because they
#                     are wrong whether or not the unit is instance-only.
#                     An unreadable allow file and an entry that matched
#                     nothing are both warned about on stderr: the first
#                     silently un-suppresses everything, the second is a line
#                     the operator is carrying that covers nothing.
#   --env-file FILE   the EnvironmentFile= every unit sources, checked for keys
#                     that shadow a drop-in Environment=
#                     (default <root>/etc/robothor/robothor.env)
#
# Environment:
#   ROBOTHOR_SYSTEMCTL  systemctl to interrogate for enabled/active state
#                       (default: systemctl, and only when --root is absent —
#                       under a test root the host's systemd knows nothing about
#                       the units being inspected, so state is reported unknown
#                       rather than guessed)
#   ROBOTHOR_WORKSPACE / ROBOTHOR_SERVICE_USER / ROBOTHOR_SERVICE_HOME /
#   ROBOTHOR_ENV_FILE   passed through to scripts/render-unit.sh.
#                       ROBOTHOR_WORKSPACE additionally extends the template
#                       search path with <workspace>/infra/systemd, so that
#                       instance-land templates — gitignored by design, and so
#                       present only in the workspace checkout — are compared
#                       rather than reported as untemplated when the doctor is
#                       run from another checkout. This checkout's templates
#                       win any name collision.
#
# Exit: 0 = no findings, 1 = findings (the summary line carries the count),
#       2 = usage error.
#
# NOT `set -e`: a doctor stops being useful the moment it reports the first
# problem and quits. Every check runs; the count decides the exit code.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/infra/systemd"
DRIFT="${REPO_ROOT}/scripts/check_dropin_drift.sh"
HOST_INSTALLER="${REPO_ROOT}/scripts/install-host-scripts.sh"

ROOT=""
ALLOW_FILE=""
ALLOW_FILE_EXPLICIT=0
UNIT_ENV_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            ROOT="${2:?--root requires a directory}"
            shift 2
            ;;
        --allow-file)
            ALLOW_FILE="${2:?--allow-file requires a file}"
            ALLOW_FILE_EXPLICIT=1
            shift 2
            ;;
        --env-file)
            UNIT_ENV_FILE="${2:?--env-file requires a file}"
            shift 2
            ;;
        *)
            echo "usage: instance_doctor.sh [--root DIR] [--allow-file FILE] [--env-file FILE]" >&2
            exit 2
            ;;
    esac
done

SYSTEM_DIR="${ROOT}/etc/systemd/system"
BIN_DIR="${ROOT}/usr/local/bin"
: "${ALLOW_FILE:=${ROOT}/etc/robothor/instance-units.allow}"
: "${UNIT_ENV_FILE:=${ROOT}/etc/robothor/robothor.env}"

# Under --root the templates must render against the same values the installer
# used, or every unit reads as drifted. Point the renderer at this root's env
# file unless the caller has already chosen one.
if [[ -n "$ROOT" && -z "${ROBOTHOR_ENV_FILE:-}" ]]; then
    export ROBOTHOR_ENV_FILE="${ROOT}/etc/robothor/robothor.env"
fi

# ── template search path ──────────────────────────────────────────────────────
# SRC_DIR is the infra/systemd of whatever checkout this script was RUN from,
# and that is not the only place a unit template legitimately lives. Some units
# are instance-land and their templates are gitignored on purpose (.gitignore
# carries /infra/systemd/delphi-*.service and
# /infra/systemd/robothor-delphi-engine.*, per CLAUDE.md rule 11), so they
# exist only in the workspace checkout that serves the box. Run out of a branch
# worktree or a fresh clone, the doctor could not see them and reported
# `no-template` for a unit that HAS one — a finding whose remedy had already
# been carried out, and which an operator can only silence by allow-listing a
# unit that is in fact templated. Two wrong answers: a false finding, and a
# suppression that then hides a real one.
#
# So the lookup is a search path rather than one directory: this checkout
# first, then the workspace's infra/systemd when it resolves somewhere else.
# This checkout wins a name collision — the tracked platform template is the
# authority, or a stale workspace copy would decide whether a reviewed change
# looks like drift.
doctor_env_lookup() {  # KEY -> value from the renderer's env file, or ""
    local key="$1" file="${ROBOTHOR_ENV_FILE:-${ROOT}/etc/robothor/robothor.env}" line
    [[ -r "$file" ]] || return 0
    line="$(grep -E "^${key}=" "$file" | tail -n 1)" || true
    printf '%s' "${line#*=}"
}

TEMPLATE_DIRS=("$SRC_DIR")
_ws="${ROBOTHOR_WORKSPACE:-$(doctor_env_lookup ROBOTHOR_WORKSPACE)}"
if [[ -n "$_ws" && -d "${_ws}/infra/systemd" ]]; then
    _ws_src="$(cd "${_ws}/infra/systemd" && pwd -P)"
    _own_src="$(cd "$SRC_DIR" && pwd -P)"
    [[ "$_ws_src" != "$_own_src" ]] && TEMPLATE_DIRS+=("$_ws_src")
fi

# First template directory that carries this relative path, or nothing.
template_for() {  # NAME|DIR/NAME -> path, exit 1 when no directory has it
    local rel="$1" dir
    for dir in "${TEMPLATE_DIRS[@]}"; do
        if [[ -e "${dir}/${rel}" ]]; then
            printf '%s' "${dir}/${rel}"
            return 0
        fi
    done
    return 1
}

# Every template across the search path, once, first directory winning. Printed
# one relative path per line so callers can read it with `while read`.
template_index() {
    local dir src rel
    for dir in "${TEMPLATE_DIRS[@]}"; do
        for src in "$dir"/robothor-*.service "$dir"/robothor-*.timer "$dir"/robothor-*.path \
                   "$dir"/robothor-*.service.d/*.conf; do
            [[ -e "$src" ]] || continue
            if [[ "$src" == *.conf ]]; then
                rel="$(basename "$(dirname "$src")")/$(basename "$src")"
            else
                rel="$(basename "$src")"
            fi
            printf '%s\n' "$rel"
        done
    done | awk '!seen[$0]++'
}

FINDINGS=0
log() { echo "[instance-doctor] $*"; }
section() { echo; echo "--- $1 ---"; }
finding() {
    FINDINGS=$((FINDINGS + 1))
    echo "FINDING [$1] $2"
}
detail() { while IFS= read -r _line; do echo "    ${_line}"; done <<<"$1"; }

shopt -s nullglob

# ── allow list ────────────────────────────────────────────────────────────────
# Instance-land by construction: the units it names (a desktop session, a
# vendor CRM) exist on one box and naming them in the repo would be exactly the
# instance data CLAUDE.md rule 1 forbids.
ALLOWED=()
declare -A ALLOW_HIT=()

# An allow file that cannot be read suppresses nothing, and the page that
# results looks exactly like a box that suddenly grew a dozen untemplated
# units. Say so, on stderr, rather than letting the operator re-triage
# findings they had already dismissed.
if [[ ! -r "$ALLOW_FILE" ]] && { [[ -e "$ALLOW_FILE" ]] || [[ $ALLOW_FILE_EXPLICIT -eq 1 ]]; }; then
    echo "[instance-doctor] WARNING: cannot read allow file ${ALLOW_FILE} — every entry in it is being ignored, so deliberately instance-only units are reported below" >&2
fi

if [[ -r "$ALLOW_FILE" ]]; then
    while IFS= read -r line; do
        line="${line%%#*}"
        line="$(tr -d '[:space:]' <<<"$line")"
        [[ -n "$line" ]] && ALLOWED+=("$line")
    done < "$ALLOW_FILE"
fi

is_allowed() {
    local needle="$1" entry
    for entry in ${ALLOWED[@]+"${ALLOWED[@]}"}; do
        if [[ "$entry" == "$needle" ]]; then
            ALLOW_HIT["$entry"]=1
            return 0
        fi
    done
    return 1
}

# An entry that matched nothing this run is a line the operator is carrying
# that covers nothing: the unit was removed, or it finally got a template. It
# reads as coverage and is not, so it gets said out loud — on stderr, because
# a stale suppression is not itself a finding about the box.
report_unused_allow_entries() {
    local entry
    for entry in ${ALLOWED[@]+"${ALLOWED[@]}"}; do
        [[ -n "${ALLOW_HIT[$entry]:-}" ]] && continue
        echo "[instance-doctor] WARNING: allow entry '${entry}' (${ALLOW_FILE}) matched nothing — the unit is gone or it now has a template; the line suppresses nothing" >&2
    done
}

# ── systemd state seam ────────────────────────────────────────────────────────
# Deliberately unset under --root with no explicit seam: the host's systemd has
# never heard of a unit staged into a temp directory, and answering "inactive"
# for it would be a fabricated fact — the exact failure this whole script
# exists to stop.
SYSTEMCTL="${ROBOTHOR_SYSTEMCTL:-}"
if [[ -z "$SYSTEMCTL" && -z "$ROOT" ]] && command -v systemctl >/dev/null 2>&1; then
    SYSTEMCTL="systemctl"
fi

unit_state() {  # unit verb(is-enabled|is-active) -> state, or "" when unknown
    [[ -n "$SYSTEMCTL" ]] || return 0
    "$SYSTEMCTL" "$2" "$1" 2>/dev/null || true
}

unit_prop() {  # unit property -> value, or "" when unknown
    [[ -n "$SYSTEMCTL" ]] || return 0
    "$SYSTEMCTL" show -p "$2" --value "$1" 2>/dev/null || true
}

# Deliberate masking (`systemctl mask` points the unit at /dev/null) is an
# operator decision, not drift. Reporting it would teach the operator to skim
# past the symlink findings that DO matter.
is_masked() {
    [[ -L "$1" && "$(readlink -f "$1")" == "/dev/null" ]]
}

# check_dropin_drift.sh also reports keys shadowed by the env file. That check
# is run once, comprehensively, in its own section below — over every live
# drop-in, not only the mirrored ones — so it is suppressed here to keep each
# finding from being reported twice.
# check_dropin_drift.sh distinguishes "these differ" (1) from "I could not
# compare these" (2 — a missing renderer, or a render env it cannot resolve).
# That distinction is passed straight through: collapsing 2 into drift sends
# the operator to reconcile a difference nobody measured, and buries the real
# fault, which is that these units are not being checked at all.
diff_against_template() {  # live mirror -> 0 = same, 1 = drift, 2 = cannot compare
    local live="$1" mirror="$2" out rc
    out="$(ENV_FILE=/nonexistent/robothor.env bash "$DRIFT" "$live" "$mirror" 2>&1)"
    rc=$?
    [[ $rc -eq 0 ]] && return 0
    printf '%s\n' "$out"
    return $rc
}

# The three call sites differ only in what they call the thing being compared.
report_comparison() {  # rc out kind name
    case "$1" in
        0) return 0 ;;
        2)
            finding "cannot-compare" \
                "${4}: could not be compared with its template — the doctor is not checking this ${3}"
            ;;
        *)
            finding "template-drift" "${4}: live ${3} differs from its rendered template"
            ;;
    esac
    detail "$2"
}

log "root=${ROOT:-/}  templates=${TEMPLATE_DIRS[*]}"
if [[ -z "$SYSTEMCTL" ]]; then
    log "NOTE: no systemctl seam — enabled/active state reported as unknown,"
    log "NOTE: and the enabled-vs-active check is skipped (not silently passed)."
fi

# ── (a) template → live ───────────────────────────────────────────────────────
section "template vs live"
while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    src="$(template_for "$rel")"
    live="${SYSTEM_DIR}/${rel}"
    if [[ "$rel" == */* ]]; then
        kind="drop-in"
    else
        kind="unit"
        is_masked "$live" && continue
        # A symlinked unit is reported by the symlink check below; diffing it
        # here would report the same fact twice.
        [[ -L "$live" ]] && continue
    fi
    if [[ ! -e "$live" ]]; then
        finding "not-installed" "${rel}: ${kind} template exists, nothing installed (run scripts/install-units.sh)"
        continue
    fi
    out="$(diff_against_template "$live" "$src")"
    report_comparison "$?" "$out" "$kind" "$rel"
done < <(template_index)

# ── (b) live without template ─────────────────────────────────────────────────
section "live units with no template"
for live in "$SYSTEM_DIR"/robothor-*.service "$SYSTEM_DIR"/robothor-*.timer "$SYSTEM_DIR"/robothor-*.path; do
    name="$(basename "$live")"
    is_masked "$live" && continue
    template_for "$name" >/dev/null && continue
    is_allowed "$name" && continue
    enabled="$(unit_state "$name" is-enabled)"
    active="$(unit_state "$name" is-active)"
    finding "no-template" \
        "${name}: live unit has no template in infra/systemd/ (enabled=${enabled:-unknown} active=${active:-unknown}) — a rebuilt box loses it"
done

for live in "$SYSTEM_DIR"/robothor-*.service.d/*.conf; do
    unit_dir="$(basename "$(dirname "$live")")"
    conf="$(basename "$live")"
    rel="${unit_dir}/${conf}"
    # onfailure.conf is generated by scripts/install_onfailure_alerts.sh, which
    # IS the repo's source of truth for it; there is deliberately no mirror.
    [[ "$conf" == "onfailure.conf" ]] && continue
    template_for "$rel" >/dev/null && continue
    is_allowed "$rel" && continue
    finding "unmirrored-dropin" \
        "${rel}: hand-written drop-in with no repo mirror — unversioned production config"
done

# ── (c) inert files in drop-in directories ────────────────────────────────────
section "inert files in drop-in directories"
for dropin_dir in "$SYSTEM_DIR"/robothor-*.service.d; do
    [[ -d "$dropin_dir" ]] || continue
    for f in "$dropin_dir"/*; do
        [[ -f "$f" ]] || continue
        [[ "$f" == *.conf ]] && continue
        finding "inert-file" \
            "$(basename "$dropin_dir")/$(basename "$f"): systemd reads *.conf only — this file does nothing but mislead whoever reads the directory"
    done
done

# ── (d) symlinked units ───────────────────────────────────────────────────────
section "symlinked units"
for live in "$SYSTEM_DIR"/robothor-*.service "$SYSTEM_DIR"/robothor-*.timer "$SYSTEM_DIR"/robothor-*.path; do
    [[ -L "$live" ]] || continue
    is_masked "$live" && continue
    finding "symlink" \
        "$(basename "$live") -> $(readlink "$live"): unit is a symlink, so it was never rendered and moving or deleting the target silently unschedules it"
done

# ── (e) enabled ≠ active ──────────────────────────────────────────────────────
section "enabled vs active"
if [[ -z "$SYSTEMCTL" ]]; then
    log "skipped — no systemctl seam"
else
    for live in "$SYSTEM_DIR"/robothor-*.service "$SYSTEM_DIR"/robothor-*.timer; do
        name="$(basename "$live")"
        is_masked "$live" && continue
        [[ "$name" == *@.service ]] && continue  # a template unit has no state
        enabled="$(unit_state "$name" is-enabled)"
        active="$(unit_state "$name" is-active)"
        [[ -n "$enabled" && -n "$active" ]] || continue
        # A oneshot fired by a timer or a path is inactive by design; so is
        # anything systemd calls static/indirect/generated. Reporting those
        # would bury the real findings under a dozen false ones.
        case "$enabled" in
            static|indirect|generated|transient|alias|masked*) continue ;;
        esac
        if [[ "$name" == *.service ]]; then
            [[ -n "$(unit_prop "$name" TriggeredBy)" ]] && continue
            [[ "$(unit_prop "$name" Type)" == "oneshot" ]] && continue
        fi
        case "${enabled}/${active}" in
            enabled/active|enabled-runtime/active|enabled/activating|enabled-runtime/activating)
                ;;
            enabled/*|enabled-runtime/*)
                finding "enabled-not-active" \
                    "${name}: enabled=${enabled} but active=${active} — it is meant to be running and is not"
                ;;
            */active|*/activating)
                finding "active-not-enabled" \
                    "${name}: active=${active} but enabled=${enabled} — it disappears at the next reboot"
                ;;
        esac
    done
fi

# ── (f) host ops script drift ─────────────────────────────────────────────────
# The pairs are read out of install-host-scripts.sh rather than restated here:
# a hand-maintained second list is how #329/#330/#331 happened.
section "host ops script drift"
while IFS= read -r line; do
    src_expr="$(awk -F'"' '{print $2}' <<<"$line")"
    dest_name="$(awk -F'"' '{print $4}' <<<"$line")"
    [[ -n "$src_expr" && -n "$dest_name" ]] || continue
    src="${REPO_ROOT}/${src_expr#\$\{REPO_ROOT\}/}"
    live="${BIN_DIR}/${dest_name}"
    if [[ ! -e "$live" ]]; then
        finding "not-installed" "${dest_name}: not installed (run scripts/install-host-scripts.sh)"
        continue
    fi
    out="$(diff_against_template "$live" "$src")"
    rc=$?
    if [[ $rc -eq 2 ]]; then
        finding "cannot-compare" \
            "${dest_name}: could not be compared with ${src_expr#\$\{REPO_ROOT\}/} — the doctor is not checking this script"
        detail "$out"
    elif [[ $rc -ne 0 ]]; then
        finding "host-script-drift" "${dest_name}: installed copy differs from ${src_expr#\$\{REPO_ROOT\}/}"
        detail "$out"
    fi
done < <(grep -E '^install_one "' "$HOST_INSTALLER" 2>/dev/null)

# ── (g) env file shadowing a drop-in Environment= ─────────────────────────────
# systemd applies EnvironmentFile= AFTER the drop-in's Environment= directives,
# so a key set in both is governed by the env file. On 2026-07-25 a flag flip
# was applied to the versioned drop-in, every drift check reported OK, and the
# running process kept the old value because robothor.env also set it.
section "env file shadowing drop-in Environment="
if [[ ! -r "$UNIT_ENV_FILE" ]]; then
    log "no env file at ${UNIT_ENV_FILE} — nothing to shadow"
else
    env_keys="$(grep -oE '^[A-Z0-9_]+=' "$UNIT_ENV_FILE" | tr -d '=' | sort -u)"
    for live in "$SYSTEM_DIR"/robothor-*.service.d/*.conf; do
        rel="$(basename "$(dirname "$live")")/$(basename "$live")"
        dropin_keys="$(grep -oE '^Environment=[A-Z0-9_]+=' "$live" | sed 's/^Environment=//; s/=$//' | sort -u)"
        [[ -n "$dropin_keys" ]] || continue
        dupes="$(comm -12 <(printf '%s\n' "$env_keys") <(printf '%s\n' "$dropin_keys"))"
        [[ -n "$dupes" ]] || continue
        while IFS= read -r key; do
            [[ -n "$key" ]] || continue
            finding "env-shadow" \
                "${rel}: ${key} is set in both ${UNIT_ENV_FILE} and this drop-in — the env file wins, so flipping it here does nothing"
        done <<<"$dupes"
    done
fi

# ── Summary ───────────────────────────────────────────────────────────────────
report_unused_allow_entries
echo
if [[ $FINDINGS -eq 0 ]]; then
    log "OK — the box matches the repo (0 findings)"
    exit 0
fi
log "${FINDINGS} finding(s) — the box and the repo disagree"
log "Reconcile: scripts/install-units.sh + scripts/install-host-scripts.sh for"
log "drift; template the untemplated units into infra/systemd/, or name them in"
log "${ALLOW_FILE} if they are deliberately instance-only."
exit 1
