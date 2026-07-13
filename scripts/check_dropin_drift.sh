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

if diff_output=$(diff -u "$MIRROR" "$LIVE"); then
    echo "drift-check: OK — live drop-in matches repo mirror"
    exit 0
fi

echo "drift-check: DRIFT — live drop-in differs from repo mirror"
echo "  live:   $LIVE"
echo "  mirror: $MIRROR"
echo "$diff_output"
echo "Reconcile: commit the live change to the mirror (see docs/runbooks/GUARDRAIL_FLIPS.md)"
exit 1
