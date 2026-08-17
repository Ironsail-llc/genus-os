#!/usr/bin/env bash
# Compare the live engine systemd drop-in against its git-versioned mirror.
#
# The drop-in carries the production guardrail/feature-flag posture; an
# unversioned edit there is invisible security state. This guard makes any
# divergence loud (guardrail-watch runs it daily).
#
# Usage: check_dropin_drift.sh [LIVE_PATH] [MIRROR_PATH]
# Exit codes: 0 = in sync, 1 = drift (diff printed), 2 = a file is missing.
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

if diff_output=$(diff -u "$MIRROR" "$LIVE"); then
    echo "drift-check: OK — live drop-in matches repo mirror, no shadowed flags"
    exit 0
fi

echo "drift-check: DRIFT — live drop-in differs from repo mirror"
echo "  live:   $LIVE"
echo "  mirror: $MIRROR"
echo "$diff_output"
echo "Reconcile: commit the live change to the mirror (see docs/runbooks/GUARDRAIL_FLIPS.md)"
exit 1
