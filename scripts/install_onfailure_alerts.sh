#!/usr/bin/env bash
# Install an OnFailure= drop-in for each named unit so a failure pages the
# operator via robothor-alert@.service (see infra/systemd/robothor-alert@.service).
#
# Usage: install_onfailure_alerts.sh [--root DIR] UNIT [UNIT...]
#   --root DIR   systemd directory to install into (default
#                /etc/systemd/system; override for tests / staged installs)
#
# Idempotent: re-running rewrites the same drop-ins. Run `systemctl
# daemon-reload` after installing into the real systemd root.
set -euo pipefail

ROOT="/etc/systemd/system"
if [[ "${1:-}" == "--root" ]]; then
    ROOT="$2"
    shift 2
fi

if [[ $# -eq 0 ]]; then
    echo "usage: install_onfailure_alerts.sh [--root DIR] UNIT [UNIT...]" >&2
    exit 1
fi

for unit in "$@"; do
    dropin_dir="${ROOT}/${unit}.d"
    mkdir -p "$dropin_dir"
    cat > "${dropin_dir}/onfailure.conf" <<'EOF'
# Installed by scripts/install_onfailure_alerts.sh — pages the operator on
# Telegram when this unit fails. Remove this file to silence.
[Unit]
OnFailure=robothor-alert@%n.service
EOF
    echo "installed ${dropin_dir}/onfailure.conf"
done
