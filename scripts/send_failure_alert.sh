#!/usr/bin/env bash
# Page the operator via Telegram when a systemd unit fails.
# Invoked by robothor-alert@<unit>.service (OnFailure= hook), so this must
# stay dependency-free: bash + curl only, credentials from the environment.
#
# Usage: send_failure_alert.sh <unit-name> [body]
#
# <unit-name> is both the headline and the dedup key. The optional [body]
# replaces the journal tail for callers that already know what went wrong
# (thermal-guard.sh, boot-guard.sh): those page under a pseudo-unit that
# journalctl has nothing for, so the tail was always "<journal unavailable>".
# Every existing caller passes one argument and is unaffected.
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

UNIT="${1:?usage: send_failure_alert.sh <unit-name> [body]}"
BODY="${2:-}"

# ── Never page from a test run ────────────────────────────────────────────────
# 2026-08-27: a suite run delivered three real Telegram alerts, including
# "2 CORRUPT offsite (bytes differ from source): robothor_memory-20260712.sql.gz"
# -- a FIXTURE filename that reads exactly like a data-integrity emergency, and
# another naming a pytest tmpdir outright. tests/test_backup_offsite.py
# subprocess-runs the real backup script with a clean env, so no pytest marker
# arrives; this pager re-sources credentials itself and delivered anyway.
#
# model_breaker._in_pytest() already guards this class in Python, added after
# 92 of 145 production escalation rows turned out to be pytest fixture models.
# This is the shell equivalent, placed at the crossing point every caller uses
# rather than in each caller.
#
# Deliberately NARROW: it suppresses only messages that name a pytest temp
# directory, never a real unit failure. An alert path that guesses would be
# worse than the spam.
if [[ "$UNIT" == *"pytest-of-"* || "$UNIT" == *"/pytest-"* ]]; then
    echo "send_failure_alert: refusing to page — message names a pytest temp path: $UNIT" >&2
    exit 0
fi
if [[ -n "${ROBOTHOR_ALERT_SUPPRESS:-}" ]]; then
    echo "send_failure_alert: suppressed by ROBOTHOR_ALERT_SUPPRESS: $UNIT" >&2
    exit 0
fi

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
COOLDOWN="${ROBOTHOR_ALERT_COOLDOWN_SECONDS:-3600}"

mkdir -p "$STATE_DIR" 2>/dev/null || true

# ── Cooldown state dir the CALLING USER can actually write ───────────────────
# /run/robothor/alert-cooldown is created root:root 0755 by the systemd units,
# and cron runs as the operator's own user. So for every cron-driven page the
# `touch` below silently failed (it is `|| true`, because a broken stamp must
# never block a real page) and the next run re-read an empty state dir and
# paged again: a crontab entry pointing at a deleted script paged once a day
# for 129 days, and the backup storm paged with no dedup at all.
#
# Fall back to a per-uid dir for BOTH the read and the stamp — moving only the
# stamp would leave the dedup half-wired, reading a dir nothing ever writes.
# Root's path is untouched: root can write /run/robothor/alert-cooldown, so
# -w is true and nothing here fires.
if [[ ! -w "$STATE_DIR" ]]; then
    if [[ -n "${ROBOTHOR_ALERT_FALLBACK_STATE_DIR:-}" ]]; then
        FALLBACK_STATE_DIR="$ROBOTHOR_ALERT_FALLBACK_STATE_DIR"
    elif [[ -n "${XDG_RUNTIME_DIR:-}" && -w "${XDG_RUNTIME_DIR}" ]]; then
        # Prefer the per-session runtime dir: it is tmpfs, per-user by
        # construction, and gone on logout/reboot like /run/robothor itself —
        # a better match than a /tmp path that outlives the session.
        FALLBACK_STATE_DIR="${XDG_RUNTIME_DIR}/robothor-alert-cooldown"
    else
        FALLBACK_STATE_DIR="/tmp/robothor-alert-cooldown-$(id -u)"
    fi
    # SC2174: with -p, -m applies only to the deepest directory. That is the
    # one that matters here — the stamps live in the leaf, and mkdir creates
    # it 0700 atomically, so no other user can read or plant stamps in it.
    # Any parent it has to create is a plain 0755 dir owned by this user, so
    # nothing else can unlink through it either.
    # shellcheck disable=SC2174
    mkdir -m 700 -p "$FALLBACK_STATE_DIR" 2>/dev/null || true
    # mkdir -p succeeds SILENTLY on a directory that already exists — it does
    # not chmod an existing leaf to 700, and it happily follows a symlink to
    # a directory. A local user who pre-creates this exact path (or symlinks
    # it elsewhere) could plant or read cooldown stamps and suppress a real
    # page. So the leaf must be re-checked after mkdir: a real directory
    # (not a symlink), owned by this user, or it is not trusted.
    #
    # A page must never be SUPPRESSED by an untrusted dir, and must never be
    # DROPPED either — so a failed check disables dedup for this send rather
    # than aborting it.
    if [[ -d "$FALLBACK_STATE_DIR" && ! -L "$FALLBACK_STATE_DIR" && -O "$FALLBACK_STATE_DIR" ]]; then
        # Name it: a cooldown that moves silently is a cooldown nobody can
        # find when they go looking for why a page did not arrive.
        echo "send_failure_alert: ${STATE_DIR} is not writable; using cooldown state dir ${FALLBACK_STATE_DIR}" >&2
        STATE_DIR="$FALLBACK_STATE_DIR"
    else
        echo "send_failure_alert: fallback state dir ${FALLBACK_STATE_DIR} is not a directory owned by this user — dedup disabled for this send" >&2
        STATE_DIR=""
    fi
fi

STAMP_FILE=""
if [[ -n "$STATE_DIR" ]]; then
    STAMP_FILE="${STATE_DIR}/${SANITIZED}.${UNIT_HASH}"
fi

if [[ -n "$STAMP_FILE" && -f "$STAMP_FILE" ]]; then
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

# ── What the operator has actually LOST ──────────────────────────────────────
# Pages read "🔴 <unit> FAILED on <host>" and nothing else. That is a fact
# about systemd, not about the operator's data: ~50 of them were scrolled past
# while every backup path was down, because the text gave no way to tell "a
# log shipper is a few minutes behind" from "there is no restorable copy of
# the database tonight". The consequence goes on line 2, inside Telegram's
# notification preview, so it is legible without opening the message.
BACKUP_STATE_DIR="${ROBOTHOR_BACKUP_STATE_DIR:-/var/lib/robothor/backup-state}"

# The newest successful run recorded by the backup scripts. An EMPTY value
# where a timestamp belongs reads as "recent"; it means the opposite, so say
# so out loud.
backup_marker() {
    local file="${BACKUP_STATE_DIR}/$1" value=""
    if [[ -r "$file" ]]; then
        value="$(head -n 1 "$file" 2>/dev/null | tr -d '\r' | cut -c1-120)"
    fi
    printf '%s' "${value:-unknown (no successful run recorded)}"
}

# First match wins, so the specific patterns come before the general ones.
# Matched as substrings because the same key arrives in three shapes: a
# systemd unit ("robothor-wal-offsite.service"), a cron pseudo-unit
# ("cron: ... wal-offsite.sh (exit 1)"), and a script's own label
# ("offsite-backup: ...").
#
# The engine and vision arms are the exception: *engine* matched
# "search-engine" and *vision* matched "provision"/"supervision", so those
# two are anchored to the real unit name instead of left as bare substrings.
consequence_for() {
    case "$1" in
        *wal-offsite*)
            echo "PITR recovery point aging past 15 min — WAL has stopped shipping; last good ship: $(backup_marker last-wal-offsite-ok)" ;;
        *backup-local*)
            echo "Nightly dump did NOT happen; newest good: $(backup_marker last-local-dump); +24h dump-tier RPO/night" ;;
        *backup-offsite*|*offsite-backup*)
            echo "Offsite NOT refreshed; a box loss restores from $(backup_marker last-offsite-ok)" ;;
        *basebackup*)
            echo "No fresh base backup; PITR must replay every WAL since $(backup_marker last-basebackup) — restore time growing nightly" ;;
        *backup-verify*)
            echo "Backups are UNVERIFIED — a corrupt archive would now go unnoticed until a restore is attempted" ;;
        robothor-engine.service)
            echo "Agents are DOWN — no scheduled runs, no heartbeat, no delivery until this is back" ;;
        *bridge*)
            echo "Inbound/outbound channel bridge is down — messages to and from the operator are not moving" ;;
        *orchestrator*)
            echo "Workflows are not being scheduled or advanced; approvals and queued work sit untouched" ;;
        *nats*)
            echo "The message fabric is down — agent mail and federation traffic are dropping, not queuing" ;;
        robothor-vision.service|robothor-vision*)
            echo "Vision capture is down — no camera events; presence and face recognition are blind" ;;
        *liveness*)
            echo "The liveness watchdog itself is down — nothing is checking whether the engine is alive" ;;
        *)
            echo "(no consequence mapped — add one in send_failure_alert.sh)" ;;
    esac
}

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

    # Rebuilt each attempt: the journal tail sharpens as the failure plays
    # out, and a backup marker can land while the loop waits out the boot
    # window.
    DETAIL="$BODY"
    if [[ -z "$DETAIL" ]]; then
        JOURNAL="$(journalctl -u "$UNIT" -n 5 --no-pager -o cat 2>/dev/null | tail -c 500 || true)"
        DETAIL="Last journal lines:
${JOURNAL:-<journal unavailable>}

Check: systemctl status ${UNIT}"
    fi
    TEXT="🔴 ${UNIT} FAILED on ${HOST}
$(consequence_for "$UNIT")
$(date -Is)

${DETAIL}"

    # --retry inside curl covers transient blips within an attempt; the outer
    # loop covers the minutes-long boot-DNS window.
    #
    # The HTTP STATUS is checked explicitly, not just curl's exit code. curl
    # exits 0 on an HTTP 401 -- it fetched the response body just fine, the body
    # simply says {"ok":false,"error_code":401}. A revoked bot token or a wrong
    # chat_id therefore read as a DELIVERED page: the stamp was touched, arming
    # a 1h cooldown on a page nobody received, and the script exited 0 so
    # systemd's Restart=on-failure never spent a retry. This is the only paging
    # path for 8 units, including the engine watchdog and offsite backup.
    http_code=$(curl -sS --max-time 15 --retry 3 --retry-all-errors --retry-delay 2 \
        -o /dev/null -w '%{http_code}' \
        --data-urlencode "chat_id=${ROBOTHOR_TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${TEXT}" \
        "${API_BASE}/bot${ROBOTHOR_TELEGRAM_BOT_TOKEN}/sendMessage" 2>/dev/null)
    curl_rc=$?
    if [ "$curl_rc" -eq 0 ] && [ "${http_code:-0}" -ge 200 ] && [ "${http_code:-0}" -lt 300 ]; then
        # Touch the stamp only AFTER a successful send, so a failed send (e.g.
        # Telegram is down) does not suppress the retry on the next failure.
        # STAMP_FILE is empty when dedup was disabled (no trustworthy state
        # dir) — nothing to touch, and nothing should be.
        if [[ -n "$STAMP_FILE" ]]; then
            mkdir -p "$STATE_DIR" 2>/dev/null || true
            touch "$STAMP_FILE" 2>/dev/null || true
        fi
        # Say so. A successful send printed NOTHING, so a cron log carried no
        # evidence a page had ever gone out — an entirely silent pager and a
        # quiet night looked identical in the logs, and only the failures were
        # ever visible.
        echo "send_failure_alert: delivered page for ${UNIT} (http ${http_code})"
        exit 0
    fi
    # Name the status: a 401 means rotate the token, a 000 means the network is
    # down. "attempt failed" alone sends the operator looking in the wrong place.
    echo "send_failure_alert: send attempt ${attempt}/${MAX_ATTEMPTS} failed" \
         "(curl_rc=${curl_rc} http_status=${http_code:-none})" >&2
done

echo "send_failure_alert: failed to send Telegram message after ${MAX_ATTEMPTS} attempts" >&2
exit 1
