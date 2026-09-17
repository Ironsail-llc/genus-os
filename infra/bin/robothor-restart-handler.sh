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
# OUTSIDE the repo. The engine runs with ReadWritePaths=<workspace>,
# so a root handler executed from inside the repo could be rewritten by an
# injected agent — exactly the escalation #205 closed. scripts/install-units.sh
# copies it to /usr/local/lib/robothor/ root:root 0755.
#
# EACH UNIT IS RESTARTED ONCE. A restart job that arrives while the same
# unit's previous restart is still starting it SUPERSEDES that start job, and
# systemd kills whatever the job was running. On the engine that is
# ExecStartPre=load-secrets.sh — a control process, whose death by SIGTERM is
# not a clean exit: `Control process exited, code=killed, status=15/TERM`,
# `Failed with result 'signal'`, OnFailure= pages (2026-09-17, twice, on a
# deploy whose two restart commands were 1.65 s apart). So this broker must
# never be the second restart:
#
#   * every request in one pass is collected first and issued as ONE
#     `systemctl restart a b c` — one transaction, in which systemd merges the
#     jobs, rather than one transaction per request file
#   * a unit that already has a job queued (`systemctl show -p Job`) is left
#     alone: that job starts it with the new code, and a restart on top of it
#     is exactly the kill described above
#   * an exclusive lock serialises two handlers, so a manual run cannot
#     interleave with the one robothor-restart.service is running
set -euo pipefail

REQUEST_DIR="${ROBOTHOR_RESTART_REQUEST_DIR:-/run/robothor/restart-requests}"

# One handler at a time. The lock is taken BEFORE any request is consumed, so
# a handler that waits here leaves the requests for itself, not for nobody.
LOCK_FILE="${ROBOTHOR_RESTART_LOCK:-/run/lock/robothor-restart.lock}"
exec 9>"$LOCK_FILE"
flock 9

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
    robothor-bridge
    robothor-app
)

# Units this pass will restart, each at most once.
wanted=()
want() {
    local u
    for u in "${wanted[@]}"; do
        [ "$u" = "$1" ] && return 0
    done
    wanted+=("$1")
}

# The original #205 trigger was a single file meaning "restart the engine".
# Agent code still writes it, so keep honouring it rather than breaking a path
# that works while this rolls out.
LEGACY_REQUEST="${ROBOTHOR_RESTART_LEGACY_REQUEST:-/run/robothor/restart-request}"
if [ -e "$LEGACY_REQUEST" ]; then
    rm -f -- "$LEGACY_REQUEST"
    logger -t robothor-restart -p daemon.notice "restart of robothor-engine.service requested (legacy trigger)" || true
    want robothor-engine
fi

shopt -s nullglob
requests=()
[ -d "$REQUEST_DIR" ] && requests=("$REQUEST_DIR"/*)
for request in "${requests[@]}"; do
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

    logger -t robothor-restart -p daemon.notice "restart of ${name}.service requested by the agent" || true
    want "$name"
done

[ ${#wanted[@]} -gt 0 ] || exit 0

# Leave out any unit that already has a job queued or running. `Job=` is empty
# when there is none, and "<id> <type>" (e.g. "4242 restart") when there is.
targets=()
for name in "${wanted[@]}"; do
    job="$(systemctl show -p Job --value "${name}.service" 2>/dev/null || true)"
    if [ -n "$job" ]; then
        logger -t robothor-restart -p daemon.notice \
            "skipping ${name}.service — a job is already queued for it (${job})" || true
        echo "robothor-restart: skipping ${name}.service — job already queued (${job})" >&2
        continue
    fi
    targets+=("${name}.service")
done

[ ${#targets[@]} -gt 0 ] || exit 0

# ONE command, one transaction. Not a loop.
logger -t robothor-restart -p daemon.notice "restarting ${targets[*]} on agent request (one transaction)" || true
systemctl restart "${targets[@]}" || \
    echo "robothor-restart: restart of ${targets[*]} failed" >&2
