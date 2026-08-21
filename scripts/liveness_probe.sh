#!/usr/bin/env bash
# Page the operator when the engine stops answering — the pager path that does
# NOT depend on systemd's OnFailure= hook.
#
# Run by robothor-liveness.timer (every 5 minutes). Dependency-free on purpose:
# bash + curl only, credentials handled by scripts/send_failure_alert.sh. It
# imports nothing from the engine it watches.
#
# WHY A SECOND PATH EXISTS
#   OnFailure= is a single, best-effort, in-band hook. Three failures in two
#   days produced total silence:
#     * 2026-08-19 13:50 — the engine was SIGKILLed during a shutdown/boot
#       transaction and systemd logged
#         robothor-engine.service: Failed to enqueue OnFailure= job, ignoring:
#         Transaction for robothor-alert@robothor-engine.service.service/start
#         is destructive (...)
#       No page was sent, and OnFailure fires exactly once — that page is gone.
#     * a wedged-but-running process never "fails" at all, so OnFailure never
#       fires and systemd's WatchdogSec only helps if the daemon's own ping
#       loop is the thing still alive.
#   This probe hits the engine from the outside and pages on what an operator
#   actually cares about: the engine is not answering.
#
# THE COUNTING DISCIPLINE
#   One failed probe is a blip (a restart, a GC pause, a slow first request).
#   Paging on blips is how a pager gets muted, so the page fires only after
#   ROBOTHOR_LIVENESS_FAILURE_THRESHOLD *consecutive* failures, and any
#   successful probe resets the count. The counter lives on tmpfs, so a reboot
#   starts the count fresh.
#
# AN UNDELIVERED PAGE IS NOT SUCCESS
#   The sender's exit status is checked, never assumed — the same discipline as
#   robothor/engine/alerts.py (`delivered = bool(sent)`), where assuming the
#   send worked hid an arity bug while 432+ alerts went nowhere. A page that
#   did not land fails this unit loudly (and leaves the counter armed, so the
#   next tick tries again).
#
# Usage: liveness_probe.sh            (no arguments; everything is env-driven)
#
# Environment:
#   ROBOTHOR_LIVENESS_URL                endpoint to probe
#                                        (default http://127.0.0.1:$ROBOTHOR_ENGINE_PORT/live —
#                                        /live is unauthenticated, see
#                                        robothor/engine/auth.py PROBE_PATHS)
#   ROBOTHOR_ENGINE_PORT                 engine port for the default URL (18800)
#   ROBOTHOR_LIVENESS_FAILURE_THRESHOLD  consecutive failures before paging (3)
#   ROBOTHOR_LIVENESS_TIMEOUT            per-probe seconds (10) — a wedged
#                                        engine accepts the connection and
#                                        never answers
#   ROBOTHOR_LIVENESS_UNIT               unit named in the page and used for the
#                                        journal tail (robothor-engine.service)
#   ROBOTHOR_LIVENESS_STATE_DIR          counter dir (/run/robothor/liveness)
#   ROBOTHOR_LIVENESS_PROBE_CMD          replaces the default curl probe
#                                        (tests, non-HTTP probes)
#   ROBOTHOR_LIVENESS_ALERT_CMD          replaces the default sender; the unit
#                                        name is appended as its last argument
set -u

log() { echo "liveness_probe: $*"; }
err() { echo "liveness_probe: $*" >&2; }

SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

UNIT="${ROBOTHOR_LIVENESS_UNIT:-robothor-engine.service}"
ENGINE_PORT="${ROBOTHOR_ENGINE_PORT:-18800}"
URL="${ROBOTHOR_LIVENESS_URL:-http://127.0.0.1:${ENGINE_PORT}/live}"
TIMEOUT="${ROBOTHOR_LIVENESS_TIMEOUT:-10}"
STATE_DIR="${ROBOTHOR_LIVENESS_STATE_DIR:-/run/robothor/liveness}"
PROBE_CMD="${ROBOTHOR_LIVENESS_PROBE_CMD:-}"
ALERT_CMD="${ROBOTHOR_LIVENESS_ALERT_CMD:-/usr/bin/env bash ${SCRIPT_DIR}/send_failure_alert.sh}"

# A non-numeric or zero threshold would either page on every blip or never page
# at all. Both are silent misconfigurations, so refuse them out loud.
THRESHOLD="${ROBOTHOR_LIVENESS_FAILURE_THRESHOLD:-3}"
if [[ ! "$THRESHOLD" =~ ^[1-9][0-9]*$ ]]; then
    err "ROBOTHOR_LIVENESS_FAILURE_THRESHOLD=${THRESHOLD} is not a positive integer"
    exit 2
fi

# Counter path, keyed per watched unit. Sanitized for use as a filename with a
# hash of the RAW name appended — systemd unit names may legally contain
# characters outside [A-Za-z0-9._-], and two different units must never share
# one counter (same reasoning as send_failure_alert.sh's cooldown stamps).
SANITIZED="$(printf '%s' "$UNIT" | tr -c 'A-Za-z0-9._-' '_')"
UNIT_HASH="$(printf '%s' "$UNIT" | sha256sum | cut -c1-8)"
COUNT_FILE="${STATE_DIR}/${SANITIZED}.${UNIT_HASH}.failures"

read_count() {
    local raw=0
    [[ -f "$COUNT_FILE" ]] && raw="$(cat "$COUNT_FILE" 2>/dev/null)"
    [[ "$raw" =~ ^[0-9]+$ ]] || raw=0
    printf '%s' "$raw"
}

write_count() {
    # A counter that cannot be persisted can never reach the threshold, which
    # would make this watchdog permanently, invisibly silent. Fail loudly
    # instead — the unit's own OnFailure= then pages about the watchdog.
    if ! mkdir -p "$STATE_DIR" 2>/dev/null || ! printf '%s\n' "$1" >"$COUNT_FILE" 2>/dev/null; then
        err "cannot write the failure counter at ${COUNT_FILE} — the watchdog cannot count"
        exit 1
    fi
}

probe() {
    if [[ -n "$PROBE_CMD" ]]; then
        # Split on whitespace deliberately: the override is a command line.
        local argv
        read -r -a argv <<<"$PROBE_CMD"
        "${argv[@]}" >/dev/null 2>&1
        return $?
    fi
    curl -fsS --max-time "$TIMEOUT" -o /dev/null "$URL" >/dev/null 2>&1
}

send_page() {
    local argv
    read -r -a argv <<<"$ALERT_CMD"
    "${argv[@]}" "$UNIT"
}

count="$(read_count)"

if probe; then
    if ((count > 0)); then
        log "${UNIT} answered ${URL} again after ${count} consecutive failed probe(s)"
    fi
    write_count 0
    exit 0
fi

count=$((count + 1))
write_count "$count"

if ((count < THRESHOLD)); then
    log "${URL} did not answer (${count}/${THRESHOLD} consecutive) — below threshold, not paging"
    exit 0
fi

err "${URL} did not answer ${count} consecutive times (threshold ${THRESHOLD}) — paging"

# Checked, not assumed. The sender exits 0 both on a delivered page and on one
# it deliberately suppressed inside its per-unit cooldown, which is what keeps
# a multi-hour outage from becoming a page storm.
if send_page; then
    log "page for ${UNIT} handed to the sender successfully"
    exit 0
fi

err "page for ${UNIT} was NOT delivered — the sender failed; leaving the counter armed to retry"
exit 1
