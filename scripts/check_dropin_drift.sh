#!/usr/bin/env bash
# Compare the live engine systemd drop-in against its git-versioned mirror.
#
# The drop-in carries the production guardrail/feature-flag posture; an
# unversioned edit there is invisible security state. This guard makes any
# divergence loud (guardrail-watch runs it daily).
#
# Render-aware: since the repo mirrors were genericized into templates
# (canonical placeholder spellings — infra/systemd/README.md), a raw diff of
# template vs live would flag every installed unit as drifted. When the
# mirror is a unit file carrying placeholders, it is first rendered through
# scripts/render-unit.sh (env: ROBOTHOR_WORKSPACE, ROBOTHOR_SERVICE_USER,
# optional ROBOTHOR_SERVICE_HOME; unset vars fall back to
# /etc/robothor/robothor.env per that script) and the RENDERED text is
# diffed against live. If the renderer is missing or the render env is
# unresolvable the check fails loudly (exit 2) — it must never silently
# report OK. Non-unit mirrors (host ops scripts) are still raw-diffed.
#
# Usage: check_dropin_drift.sh [LIVE_PATH] [MIRROR_PATH]
# Exit codes: 0 = in sync, 1 = drift (diff printed), 2 = a file is missing
#             or the templated mirror cannot be rendered.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE="${1:-/etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf}"
MIRROR="${2:-${REPO_ROOT}/infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf}"

if [[ ! -f "$LIVE" ]]; then
    echo "drift-check: live drop-in missing: $LIVE"
    exit 2
fi
if [[ ! -f "$MIRROR" ]]; then
    echo "drift-check: repo mirror missing: $MIRROR"
    exit 2
fi

# A matching mirror is necessary but not sufficient. systemd applies
# EnvironmentFile= after the drop-in's Environment= directives, so any variable
# set in BOTH /etc/robothor/robothor.env and the drop-in is governed by the env
# file — and this script would still print OK.
#
# That is not hypothetical. On 2026-07-25 the router revert was applied to the
# drop-in, the mirror matched, this check reported OK, and the flag stayed at
# its old value in the running process because robothor.env also set it. The
# env file is instance data (secrets, tenant ids) so it cannot be mirrored into
# the repo; the fix is to keep each flag in exactly one place.
ENV_FILE="${ENV_FILE:-/etc/robothor/robothor.env}"
if [[ -f "$ENV_FILE" ]]; then
    dupes=$(comm -12 \
        <(grep -oE '^[A-Z0-9_]+=' "$ENV_FILE" | tr -d '=' | sort -u) \
        <(grep -oE '^Environment=[A-Z0-9_]+=' "$LIVE" | sed 's/^Environment=//; s/=$//' | sort -u))
    if [[ -n "$dupes" ]]; then
        echo "drift-check: SHADOWED — set in both $ENV_FILE and the drop-in:"
        while read -r name; do
            [[ -z "$name" ]] && continue
            envval=$(grep -oP "^${name}=\K.*" "$ENV_FILE" | head -1)
            dropval=$(grep -oP "^Environment=${name}=\K.*" "$LIVE" | head -1)
            echo "  $name: env=${envval} dropin=${dropval}  <-- env wins"
        done <<< "$dupes"
        echo "Remove these from $ENV_FILE so the versioned drop-in governs,"
        echo "or a flip applied to the drop-in will silently do nothing."
        exit 1
    fi
fi

# ── Render the mirror when it is a placeholder-bearing unit template ─────────
# Placeholder detection matches exactly what render-unit.sh substitutes in
# non-comment lines. Extension-gated so host ops scripts (*.sh), whose bash
# ${ROBOTHOR_*} syntax is not a template placeholder, keep the raw diff.
RENDERER="${REPO_ROOT}/scripts/render-unit.sh"
COMPARE_MIRROR="$MIRROR"
MIRROR_LABEL="mirror"

mirror_has_placeholders() {
    local stripped
    stripped="$(sed 's/^[[:space:]]*[#;].*//' "$MIRROR")"
    grep -q -e '/opt/robothor' -e '/home/robothor' -e '%h' <<<"$stripped" && return 0
    grep -qE '^(User|Group)=robothor$' <<<"$stripped" && return 0
    return 1
}

case "$MIRROR" in
    *.conf|*.service|*.timer|*.path)
        if mirror_has_placeholders; then
            if [[ ! -f "$RENDERER" ]]; then
                echo "drift-check: renderer missing: $RENDERER — cannot compare templated mirror $MIRROR"
                exit 2
            fi
            RENDERED_TMP="$(mktemp)"
            trap 'rm -f "$RENDERED_TMP"' EXIT
            if ! render_err="$(bash "$RENDERER" "$MIRROR" "$RENDERED_TMP" 2>&1)"; then
                echo "drift-check: cannot render templated mirror $MIRROR:"
                echo "$render_err"
                echo "Set ROBOTHOR_WORKSPACE / ROBOTHOR_SERVICE_USER (or provide /etc/robothor/robothor.env)."
                exit 2
            fi
            COMPARE_MIRROR="$RENDERED_TMP"
            MIRROR_LABEL="mirror (rendered)"
        fi
        ;;
esac

if diff_output=$(diff -u --label "$MIRROR_LABEL: $MIRROR" --label "live: $LIVE" "$COMPARE_MIRROR" "$LIVE"); then
    echo "drift-check: OK — live drop-in matches repo mirror, no shadowed flags"
    exit 0
fi

echo "drift-check: DRIFT — live drop-in differs from repo mirror"
echo "  live:   $LIVE"
echo "  ${MIRROR_LABEL}: $MIRROR"
echo "$diff_output"
echo "Reconcile: commit the live change to the mirror (see docs/runbooks/GUARDRAIL_FLIPS.md)"
exit 1
