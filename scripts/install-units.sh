#!/usr/bin/env bash
# Install the systemd unit templates from infra/systemd/ into their live
# location, replacing the hand-copy-and-edit workflow.
#
# Sibling of scripts/install-host-scripts.sh, same failure mode: installed
# units were hand-edited copies, so template fixes in the repo never reached
# the box and the templates themselves drifted until they no longer rendered
# (`systemd-analyze verify` fails outright on robothor-engine.service's raw
# `${ROBOTHOR_WORKSPACE}` ExecStart lines).
#
# Each robothor-*.{service,timer,path} template and each
# robothor-*.service.d/*.conf drop-in is rendered via scripts/render-unit.sh
# (which enforces the structural gate: no unexpanded placeholders, no %h),
# .service files are then gated on `systemd-analyze verify`, and the results
# installed idempotently to <root>/etc/systemd/system/.
#
# Only robothor-* units are installed. delphi-* units are instance-land —
# some deliberately tombstoned — and must never be resurrected by a platform
# installer.
#
# Usage: install-units.sh [--root DIR] [--env-file FILE]
#   --root DIR      filesystem root to install under (default /, so units land
#                   at /etc/systemd/system; override for tests). Under --root,
#                   `systemd-analyze verify` is skipped: verify checks that
#                   ExecStart binaries exist, which only means something on the
#                   target box. The renderer's structural gate still runs.
#   --env-file FILE file to resolve unset ROBOTHOR_* vars from
#                   (default /etc/robothor/robothor.env)
#
# Environment: ROBOTHOR_WORKSPACE, ROBOTHOR_SERVICE_USER (required),
# ROBOTHOR_SERVICE_HOME (optional) — see scripts/render-unit.sh.
#
# Idempotent: re-running reports "unchanged" for units that already match,
# and only rewrites the ones that don't. Does not daemon-reload or restart
# anything — it prints the follow-up commands instead.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER="${REPO_ROOT}/scripts/render-unit.sh"
SRC_DIR="${REPO_ROOT}/infra/systemd"
ROOT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            ROOT="${2:?--root requires a directory}"
            shift 2
            ;;
        --env-file)
            export ROBOTHOR_ENV_FILE="${2:?--env-file requires a file}"
            shift 2
            ;;
        *)
            echo "usage: install-units.sh [--root DIR] [--env-file FILE]" >&2
            exit 1
            ;;
    esac
done

SYSTEM_DIR="${ROOT}/etc/systemd/system"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log() { echo "[install-units] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ── Render every template first ──────────────────────────────────────────────
# All-or-nothing: a render failure aborts before anything touches the target,
# so a half-updated unit set cannot exist.
shopt -s nullglob
templates=(
    "$SRC_DIR"/robothor-*.service
    "$SRC_DIR"/robothor-*.timer
    "$SRC_DIR"/robothor-*.path
)
[[ ${#templates[@]} -gt 0 ]] || die "no robothor-* unit templates found in ${SRC_DIR}"

rendered_rel=()
for src in "${templates[@]}"; do
    name="$(basename "$src")"
    bash "$RENDER" "$src" "${TMP_DIR}/${name}" || die "render failed for ${name}"
    rendered_rel+=("$name")
done
for dropin in "$SRC_DIR"/robothor-*.service.d/*.conf; do
    rel="$(basename "$(dirname "$dropin")")/$(basename "$dropin")"
    mkdir -p "${TMP_DIR}/$(dirname "$rel")"
    bash "$RENDER" "$dropin" "${TMP_DIR}/${rel}" || die "render failed for ${rel}"
    rendered_rel+=("$rel")
done

# ── Verify gate ───────────────────────────────────────────────────────────────
# Skipped under --root (test mode) and when systemd-analyze is absent: verify
# resolves ExecStart binaries and referenced units against THIS box, so the
# full check only means something at real install time. The renderer's
# structural gate (no unexpanded placeholders, no %h, parseable content) has
# already run on every file above.
if [[ -z "$ROOT" ]] && command -v systemd-analyze >/dev/null 2>&1; then
    services=( "$TMP_DIR"/robothor-*.service )
    verify=( systemd-analyze verify )
    # Template units (robothor-alert@.service) need an instance to verify;
    # --instance exists since systemd 253. On older systemd, verify the
    # non-template services only.
    if systemd-analyze --help 2>&1 | grep -q -- '--instance'; then
        verify+=( --instance=verify )
    else
        log "systemd-analyze lacks --instance; skipping verify for template units"
        filtered=()
        for s in "${services[@]}"; do
            [[ "$(basename "$s")" == *@.service ]] || filtered+=("$s")
        done
        services=( "${filtered[@]}" )
    fi
    "${verify[@]}" "${services[@]}" || die "systemd-analyze verify failed — nothing installed"
    log "systemd-analyze verify passed (${#services[@]} services)"
else
    log "skipping systemd-analyze verify (--root test mode or systemd-analyze absent)"
fi

# ── Install ───────────────────────────────────────────────────────────────────
install_one() {
    local rel="$1"
    local src="${TMP_DIR}/${rel}" dest="${SYSTEM_DIR}/${rel}"
    mkdir -p "$(dirname "$dest")"
    if [[ ! -f "$dest" ]]; then
        install -m 0644 "$src" "$dest"
        log "installed ${dest}"
    elif ! cmp -s "$src" "$dest"; then
        install -m 0644 "$src" "$dest"
        log "updated ${dest}"
    else
        chmod 0644 "$dest"
        log "unchanged ${dest}"
    fi
}

for rel in "${rendered_rel[@]}"; do
    install_one "$rel"
done

# ── Post-install invariants ───────────────────────────────────────────────────
# Every service sources /etc/robothor/robothor.env; a missing file means
# nothing starts. Warn here, once, rather than letting each unit fail at boot.
if [[ ! -r "${ROOT}/etc/robothor/robothor.env" ]]; then
    log "WARNING: ${ROOT}/etc/robothor/robothor.env does not exist — the units"
    log "WARNING: reference it via EnvironmentFile= and will fail to start."
    log "WARNING: Copy infra/systemd/robothor.env.example there and fill it in."
fi

if [[ -z "$ROOT" ]]; then
    log "next: sudo systemctl daemon-reload  (then restart the changed units)"
fi
log "done (${#rendered_rel[@]} units)"
