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
#   ROBOTHOR_ENV_FILE   passed through to scripts/render-unit.sh
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
UNIT_ENV_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            ROOT="${2:?--root requires a directory}"
            shift 2
            ;;
        --allow-file)
            ALLOW_FILE="${2:?--allow-file requires a file}"
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
        [[ "$entry" == "$needle" ]] && return 0
    done
    return 1
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
diff_against_template() {  # live mirror -> 0 = same, 1 = drift/missing
    local live="$1" mirror="$2" out rc
    out="$(ENV_FILE=/nonexistent/robothor.env bash "$DRIFT" "$live" "$mirror" 2>&1)"
    rc=$?
    [[ $rc -eq 0 ]] && return 0
    printf '%s\n' "$out"
    return 1
}

log "root=${ROOT:-/}  templates=${SRC_DIR}"
if [[ -z "$SYSTEMCTL" ]]; then
    log "NOTE: no systemctl seam — enabled/active state reported as unknown,"
    log "NOTE: and the enabled-vs-active check is skipped (not silently passed)."
fi

# ── (a) template → live ───────────────────────────────────────────────────────
section "template vs live"
for src in "$SRC_DIR"/robothor-*.service "$SRC_DIR"/robothor-*.timer "$SRC_DIR"/robothor-*.path; do
    name="$(basename "$src")"
    live="${SYSTEM_DIR}/${name}"
    if is_masked "$live"; then
        continue
    fi
    if [[ -L "$live" ]]; then
        continue  # reported by the symlink check below; a diff would repeat it
    fi
    if [[ ! -e "$live" ]]; then
        finding "not-installed" "${name}: template exists, nothing installed (run scripts/install-units.sh)"
        continue
    fi
    if ! out="$(diff_against_template "$live" "$src")"; then
        finding "template-drift" "${name}: live unit differs from its rendered template"
        detail "$out"
    fi
done

for src in "$SRC_DIR"/robothor-*.service.d/*.conf; do
    rel="$(basename "$(dirname "$src")")/$(basename "$src")"
    live="${SYSTEM_DIR}/${rel}"
    if [[ ! -e "$live" ]]; then
        finding "not-installed" "${rel}: drop-in template exists, nothing installed"
        continue
    fi
    if ! out="$(diff_against_template "$live" "$src")"; then
        finding "template-drift" "${rel}: live drop-in differs from its rendered template"
        detail "$out"
    fi
done

# ── (b) live without template ─────────────────────────────────────────────────
section "live units with no template"
for live in "$SYSTEM_DIR"/robothor-*.service "$SYSTEM_DIR"/robothor-*.timer "$SYSTEM_DIR"/robothor-*.path; do
    name="$(basename "$live")"
    is_masked "$live" && continue
    [[ -e "$SRC_DIR/$name" ]] && continue
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
    [[ -e "$SRC_DIR/$rel" ]] && continue
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
    if ! out="$(diff_against_template "$live" "$src")"; then
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
