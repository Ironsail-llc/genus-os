#!/usr/bin/env bash
# Page the operator via Telegram when a systemd unit fails.
# Invoked by robothor-alert@<unit>.service (OnFailure= hook), so this must
# stay dependency-free: bash + curl only, credentials from the environment.
#
# Usage: send_failure_alert.sh <unit-name>
#
# systemd fires OnFailure= exactly ONCE per failure — if this script cannot
# deliver the page, the page is gone forever. That is precisely what happened
# on the 2026-08-19 boot: one page died on "Could not resolve host:
# api.telegram.org" (DNS not up yet) and another on "ROBOTHOR_TELEGRAM_BOT_TOKEN
# is not set" (secrets not decrypted yet). Both failures were real; neither
# reached the operator. So the send is a bounded retry loop (default 10
# attempts, 30s apart, ~5min total) and the secrets are re-sourced INSIDE the
# loop — the boot window that breaks the send is the same window that ends a
# few seconds later.
set -u

UNIT="${1:?usage: send_failure_alert.sh <unit-name>}"

# ── Retry policy ──────────────────────────────────────────────────────────────
# Overridable so tests run fast and callers with different latency budgets
# (e.g. cron-wrapper.sh, which must not stall a cron job for 5 minutes) can
# tighten the loop.
MAX_ATTEMPTS="${ROBOTHOR_ALERT_MAX_ATTEMPTS:-10}"
RETRY_DELAY="${ROBOTHOR_ALERT_RETRY_DELAY:-30}"
# Overridable so a hermetic test/CI stub can receive the POST instead of the
# real Telegram API.
API_BASE="${ROBOTHOR_TELEGRAM_API_BASE:-https://api.telegram.org}"

# ── Cooldown: dedup repeated pages for the same unit ──────────────────────────
# A unit crash-looping on a short timer (e.g. every 15 minutes) would otherwise
# page the operator dozens of times a day for the same underlying failure —
# exactly what happened during today's incident. Stamp files are keyed per
# unit under a state dir that, by default, lives on tmpfs (matching where the
# secrets live), so the cooldown naturally clears on reboot.
STATE_DIR="${ROBOTHOR_ALERT_STATE_DIR:-/run/robothor/alert-cooldown}"
# Sanitized for use as a filename. systemd unit names can legally contain
# characters outside [A-Za-z0-9._-] unescaped in %i values (e.g. ':' and
# '\' — see man systemd.unit, systemd-escape), which the sanitize step
# below collapses to '_'. Two different units can sanitize to the same
# string (e.g. "robothor-backup:primary.service" and
# "robothor-backup_primary.service" both become
# "robothor-backup_primary.service"), so a hash of the RAW name is appended
# to disambiguate them — otherwise one unit's cooldown could suppress a
# genuine page for an unrelated unit.
SANITIZED="$(printf '%s' "$UNIT" | tr -c 'A-Za-z0-9._-' '_')"
UNIT_HASH="$(printf '%s' "$UNIT" | sha256sum | cut -c1-8)"
STAMP_FILE="${STATE_DIR}/${SANITIZED}.${UNIT_HASH}"
COOLDOWN="${ROBOTHOR_ALERT_COOLDOWN_SECONDS:-3600}"

mkdir -p "$STATE_DIR" 2>/dev/null || true

if [[ -f "$STAMP_FILE" ]]; then
    NOW=$(date +%s)
    STAMP_TIME=$(stat -c %Y "$STAMP_FILE" 2>/dev/null || echo 0)
    AGE=$(( NOW - STAMP_TIME ))
    if (( AGE < COOLDOWN )); then
        echo "send_failure_alert: suppressed duplicate page for ${UNIT} (${AGE}s < ${COOLDOWN}s)"
        exit 0
    fi
fi

# ── Credentials recovery ──────────────────────────────────────────────────────
# The credentials live ONLY in /run/robothor/secrets.env, and /run is tmpfs — so
# on a cold boot the file does not exist until some service's ExecStartPre
# decrypts it. That is exactly when services fail to start, which is exactly when
# this pager is supposed to fire. Observed on the 2026-07-14 reboot: five units
# failed and every alert died with "ROBOTHOR_TELEGRAM_BOT_TOKEN is not set".
#
# An alert that is silent during a boot failure is worse than no alert. This unit
# runs as root and the age key is root-readable, so recover the secrets rather
# than give up. Called on every retry attempt: the secrets that were missing on
# attempt 1 are usually decrypted by attempt 2 or 3.
DEFAULT_SECRETS="/run/robothor/secrets.env"
source_secrets() {
    [[ -n "${ROBOTHOR_TELEGRAM_BOT_TOKEN:-}" ]] && return 0
    # Overridable so the suite can point this at a fixture. Without that, a test
    # run on the live box would source the REAL secrets and page the operator for
    # real — the same class of accident as the benchmark runner that sent actual
    # emails.
    local secrets="${ROBOTHOR_SECRETS_FILE:-$DEFAULT_SECRETS}"
    # The decrypt fallback only ever writes the default path, so invoking it
    # for an overridden ROBOTHOR_SECRETS_FILE would be pointless (and would let
    # a test run touch the real secrets machinery).
    if [[ ! -r "$secrets" && "$secrets" == "$DEFAULT_SECRETS" ]]; then
        local decrypt
        decrypt="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/decrypt-secrets.sh"
        if [[ -x "$decrypt" ]]; then
            "$decrypt" >/dev/null 2>&1 || true
        fi
    fi
    if [[ -r "$secrets" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$secrets"
        set +a
    fi
}

HOST="$(hostname -s 2>/dev/null || echo unknown-host)"

# ── Bounded retry loop ────────────────────────────────────────────────────────
attempt=0
while (( attempt < MAX_ATTEMPTS )); do
    attempt=$(( attempt + 1 ))
    if (( attempt > 1 )); then
        sleep "$RETRY_DELAY"
    fi

    source_secrets

    if [[ -z "${ROBOTHOR_TELEGRAM_BOT_TOKEN:-}" ]]; then
        echo "send_failure_alert: ROBOTHOR_TELEGRAM_BOT_TOKEN is not set (attempt ${attempt}/${MAX_ATTEMPTS})" >&2
        continue
    fi
    if [[ -z "${ROBOTHOR_TELEGRAM_CHAT_ID:-}" ]]; then
        echo "send_failure_alert: ROBOTHOR_TELEGRAM_CHAT_ID is not set (attempt ${attempt}/${MAX_ATTEMPTS})" >&2
        continue
    fi

    # Rebuilt each attempt: the journal tail sharpens as the failure plays out.
    JOURNAL="$(journalctl -u "$UNIT" -n 5 --no-pager -o cat 2>/dev/null | tail -c 500 || true)"
    TEXT="🔴 ${UNIT} FAILED on ${HOST}
$(date -Is)

Last journal lines:
${JOURNAL:-<journal unavailable>}

Check: systemctl status ${UNIT}"

    # --retry inside curl covers transient blips within an attempt; the outer
    # loop covers the minutes-long boot-DNS window.
    if curl -sS --max-time 15 --retry 3 --retry-all-errors --retry-delay 2 \
        --data-urlencode "chat_id=${ROBOTHOR_TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${TEXT}" \
        "${API_BASE}/bot${ROBOTHOR_TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null; then
        # Touch the stamp only AFTER a successful send, so a failed send (e.g.
        # Telegram is down) does not suppress the retry on the next failure.
        mkdir -p "$STATE_DIR" 2>/dev/null || true
        touch "$STAMP_FILE" 2>/dev/null || true
        exit 0
    fi
    echo "send_failure_alert: send attempt ${attempt}/${MAX_ATTEMPTS} failed" >&2
done

echo "send_failure_alert: failed to send Telegram message after ${MAX_ATTEMPTS} attempts" >&2
exit 1
