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
#                                        (and `--drain` for the spool drain)
#   ROBOTHOR_ALERT_SPOOL_DIR             the sender's spool, watched for the
#                                        `.stuck` marker
#                                        (/var/lib/robothor/alert-spool)
#   ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS  how long a `.stuck` marker may stand
#                                        before it is a probe failure (1800)
#
# THE TICK IS ALSO THE SPOOL DRAIN
#   The sender parks a page it could not deliver in a durable spool (DNS loss
#   produced 63 `curl_rc=6` lines since 2026-08-31). Something has to come back
#   for it, and the callers that lose pages — cron-wrapper.sh,
#   backup-offsite.sh, thermal-guard.sh, boot-guard.sh — have no retrying unit
#   behind them. This one does: root, every 5 minutes, After=network-online.
#   So the FIRST thing each tick does is `send_failure_alert.sh --drain`,
#   before the probe and regardless of its outcome — a healthy engine is
#   exactly when a stranded page would otherwise sit unnoticed for days.
#
# AND THE DRAIN CANNOT REPORT ITSELF
#   `--drain` exits 0 whatever happens, deliberately: a backlog is not an
#   incident, and failing this unit over one would fire its own OnFailure=
#   page about the outage that filled the spool. The cost was that a dead
#   credential or a day-old queue produced journal lines and nothing else.
#   So the sender marks a queue it cannot move (`<spool>/.stuck`, cleared by
#   the next delivered page) and this probe turns a marker that has stood for
#   ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS into a probe failure on its own key —
#   same counting, same sender, same OnFailure= underneath. Its own key,
#   because sharing the engine's counter would let a recovering engine reset
#   the spool's count every tick. The page is one short line: the delivery
#   path is the thing that is broken, so the least is the most likely to get
#   through, and if even that fails the unit goes `failed` and OnFailure= is
#   what carries it.
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

# The sender marks a spool it cannot move here; this tick is what makes that
# marker audible. 30 minutes ≈ 6 drain attempts: long enough that a DNS blip
# or a Telegram wobble has cleared on its own, short enough that a rotated
# token is not an overnight silence.
SPOOL_DIR="${ROBOTHOR_ALERT_SPOOL_DIR:-/var/lib/robothor/alert-spool}"
STUCK_NOTE="${SPOOL_DIR}/.stuck"
STUCK_KEY="alert-spool-stuck"
STUCK_MAX_AGE="${ROBOTHOR_LIVENESS_STUCK_AGE_SECONDS:-1800}"

# A non-numeric or zero threshold would either page on every blip or never page
# at all. Both are silent misconfigurations, so refuse them out loud.
THRESHOLD="${ROBOTHOR_LIVENESS_FAILURE_THRESHOLD:-3}"
if [[ ! "$THRESHOLD" =~ ^[1-9][0-9]*$ ]]; then
    err "ROBOTHOR_LIVENESS_FAILURE_THRESHOLD=${THRESHOLD} is not a positive integer"
    exit 2
fi

# Counter path, keyed per watched thing. Sanitized for use as a filename with a
# hash of the RAW name appended — systemd unit names may legally contain
# characters outside [A-Za-z0-9._-], and two different units must never share
# one counter (same reasoning as send_failure_alert.sh's cooldown stamps).
#
# Keyed, not hardcoded to $UNIT, because the tick counts two independent
# things: whether the engine answers, and whether the alert spool is moving.
# One counter for both would let a recovering engine reset the spool's count
# on every tick.
count_file_for() {
    local key="$1" sanitized hash
    sanitized="$(printf '%s' "$key" | tr -c 'A-Za-z0-9._-' '_')"
    hash="$(printf '%s' "$key" | sha256sum | cut -c1-8)"
    printf '%s' "${STATE_DIR}/${sanitized}.${hash}.failures"
}

COUNT_FILE="$(count_file_for "$UNIT")"

read_count() {
    local raw=0
    [[ -f "$1" ]] && raw="$(cat "$1" 2>/dev/null)"
    [[ "$raw" =~ ^[0-9]+$ ]] || raw=0
    printf '%s' "$raw"
}

write_count() {
    # A counter that cannot be persisted can never reach the threshold, which
    # would make this watchdog permanently, invisibly silent. Fail loudly
    # instead — the unit's own OnFailure= then pages about the watchdog.
    if ! mkdir -p "$STATE_DIR" 2>/dev/null || ! printf '%s\n' "$2" >"$1" 2>/dev/null; then
        err "cannot write the failure counter at ${1} — the watchdog cannot count"
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

# The sender's two-argument form: <dedup key> <body>. The body IS the page,
# so nothing is prepended to it — which is what keeps a stuck-spool report
# from arriving as "🔴 alert-spool-stuck FAILED" over a journal tail.
send_page_with_body() {
    local key="$1" body="$2" argv
    read -r -a argv <<<"$ALERT_CMD"
    "${argv[@]}" "$key" "$body"
}

# ── The drain cannot report itself ───────────────────────────────────────────
# See the header. Returns non-zero only when a page was owed and could not be
# delivered — then the unit must fail, so systemd's OnFailure= is what carries
# the news that the pager is broken.
check_stuck_spool() {
    local file count mtime age now reason
    file="$(count_file_for "$STUCK_KEY")"
    count="$(read_count "$file")"

    if [[ ! -f "$STUCK_NOTE" ]]; then
        if (( count > 0 )); then
            log "the alert spool is moving again after ${count} stuck tick(s)"
            write_count "$file" 0
        fi
        return 0
    fi

    now="$(date +%s)"
    mtime="$(stat -c %Y "$STUCK_NOTE" 2>/dev/null || true)"
    [[ "$mtime" =~ ^[0-9]+$ ]] || mtime="$now"
    age=$(( now - mtime ))
    if (( age < STUCK_MAX_AGE )); then
        log "the alert spool reports itself stuck (${age}s), under the ${STUCK_MAX_AGE}s grace — not paging yet"
        return 0
    fi

    count=$(( count + 1 ))
    write_count "$file" "$count"
    if (( count < THRESHOLD )); then
        log "alert spool stuck for ${age}s (${count}/${THRESHOLD} consecutive) — below threshold, not paging"
        return 0
    fi

    # One line, trimmed: whatever is broken about delivery, a short page is
    # the one most likely to survive it.
    reason="$(head -n 1 "$STUCK_NOTE" 2>/dev/null | tr -d '\r' | cut -c1-120)"
    err "the alert spool has been stuck for ${age}s — paging on ${STUCK_KEY}"
    if send_page_with_body "$STUCK_KEY" \
        "🔴 alert spool STUCK $(( age / 60 ))m: ${reason:-no reason recorded}"; then
        return 0
    fi
    err "the stuck-spool page was NOT delivered — the sender cannot report its own outage; failing the unit so OnFailure= fires"
    return 1
}

# Best-effort by construction: a spool that could not be drained is a backlog
# still waiting, not an incident. Failing the unit here would fire its own
# OnFailure= page about the very outage that filled the spool.
drain_spool() {
    local argv
    read -r -a argv <<<"$ALERT_CMD"
    if "${argv[@]}" --drain; then
        return 0
    fi
    err "the alert spool drain reported a failure — spooled pages are still waiting"
}

drain_spool

# A stuck spool is its own failure, counted separately and reported here even
# when the engine is perfectly healthy — a healthy engine is exactly when a
# stranded queue goes unnoticed. Its exit status rides along to the end: the
# probe below is the more urgent question and must not be skipped over it.
STUCK_PAGE_FAILED=0
check_stuck_spool || STUCK_PAGE_FAILED=1

count="$(read_count "$COUNT_FILE")"

if probe; then
    if ((count > 0)); then
        log "${UNIT} answered ${URL} again after ${count} consecutive failed probe(s)"
    fi
    write_count "$COUNT_FILE" 0
    exit "$STUCK_PAGE_FAILED"
fi

count=$((count + 1))
write_count "$COUNT_FILE" "$count"

if ((count < THRESHOLD)); then
    log "${URL} did not answer (${count}/${THRESHOLD} consecutive) — below threshold, not paging"
    exit "$STUCK_PAGE_FAILED"
fi

err "${URL} did not answer ${count} consecutive times (threshold ${THRESHOLD}) — paging"

# Checked, not assumed. The sender exits 0 both on a delivered page and on one
# it deliberately suppressed inside its per-unit cooldown, which is what keeps
# a multi-hour outage from becoming a page storm.
if send_page; then
    log "page for ${UNIT} handed to the sender successfully"
    exit "$STUCK_PAGE_FAILED"
fi

err "page for ${UNIT} was NOT delivered — the sender failed; leaving the counter armed to retry"
exit 1
