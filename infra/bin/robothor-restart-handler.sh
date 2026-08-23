#!/usr/bin/env bash
# Restart broker: the agent may ASK for a restart; it may not choose the target.
#
# Runs as root from robothor-restart.service. PR #205 built that path so the
# agent could request its own restart without ever holding privilege, with one
# invariant:
#
#   The target unit is HARDCODED. It is never read from the trigger file: that
#   file is agent-writable, and letting its contents name a unit would hand an
#   injected agent the ability to stop or restart anything on the machine.
#
# The operator works from SSH and is never at the box, so the agent needs the
# same treatment for a few more units — it had been asking him to run
# `sudo systemctl restart robothor-delphi-engine.service` by hand. This grows
# the list WITHOUT weakening the invariant:
#
#   * the request is a FILENAME, matched against the fixed list below
#   * file CONTENTS are never read, never executed, never used to name a unit
#   * anything not on the list is discarded, so a bogus name cannot loop the
#     path unit
#
# INSTALL LOCATION MATTERS. This must be installed to a root-owned directory
# OUTSIDE the repo. The engine runs with ReadWritePaths=/home/philip/robothor,
# so a root handler executed from inside the repo could be rewritten by an
# injected agent — exactly the escalation #205 closed. scripts/install-units.sh
# copies it to /usr/local/lib/robothor/ root:root 0755.
set -euo pipefail

REQUEST_DIR="${ROBOTHOR_RESTART_REQUEST_DIR:-/run/robothor/restart-requests}"

# The complete set of units the agent may restart. Adding a line here is a
# deliberate grant of remote power over that unit — review it as such.
#
# NOT PRESENT, deliberately:
#   robothor-vision / mediamtx-webcam — disabled by hand after the 2026-08-19
#     GPU thermal event. Letting the agent re-enable them unattended would let
#     it undo a thermal-safety decision on a box the operator cannot physically
#     reach. That stays a human action.
#   anything not owned by this platform (sshd, tailscaled, postgresql, docker)
#     — losing those loses the operator's only route back in.
ALLOWED=(
    robothor-engine
    robothor-delphi-engine
    robothor-bridge
    robothor-app
)

# The original #205 trigger was a single file meaning "restart the engine".
# Agent code still writes it, so keep honouring it rather than breaking a path
# that works while this rolls out.
LEGACY_REQUEST="${ROBOTHOR_RESTART_LEGACY_REQUEST:-/run/robothor/restart-request}"
if [ -e "$LEGACY_REQUEST" ]; then
    rm -f -- "$LEGACY_REQUEST"
    logger -t robothor-restart -p daemon.notice "restarting robothor-engine.service (legacy trigger)" || true
    systemctl restart robothor-engine.service || \
        echo "robothor-restart: legacy restart of robothor-engine.service failed" >&2
fi

[ -d "$REQUEST_DIR" ] || exit 0

shopt -s nullglob
for request in "$REQUEST_DIR"/*; do
    name="$(basename -- "$request")"

    # Consume FIRST, always. A request left behind — honoured or refused —
    # re-triggers the path unit forever.
    rm -f -- "$request"

    permitted=0
    for unit in "${ALLOWED[@]}"; do
        [ "$name" = "$unit" ] && permitted=1 && break
    done

    if [ "$permitted" -ne 1 ]; then
        # Loud, not silent: a refused request is either a bug or an attempt.
        logger -t robothor-restart -p daemon.warning \
            "refused restart request for '${name}' — not in the allowlist" || true
        echo "robothor-restart: refused '${name}' (not allowlisted)" >&2
        continue
    fi

    logger -t robothor-restart -p daemon.notice "restarting ${name}.service on agent request" || true
    systemctl restart "${name}.service" || {
        echo "robothor-restart: restart of ${name}.service failed" >&2
        continue
    }
done
