#!/usr/bin/env bash
# Page the operator via Telegram when a systemd unit fails.
# Invoked by robothor-alert@<unit>.service (OnFailure= hook), so this must
# stay dependency-free: bash + curl only, credentials from the environment.
#
# Usage: send_failure_alert.sh <unit-name> [body]
#        send_failure_alert.sh --drain
#
# With one argument, <unit-name> is both the headline and the dedup key and
# the page is composed from it: "🔴 <unit> FAILED on <host>", the consequence
# line, and the tail of that unit's journal.
#
# With two, [body] IS the page — the caller already knows what to say, and the
# only thing added is a "<timestamp> on <host>" trailer. <unit-name> stays the
# dedup key and nothing else: a caller sending a RECOVERY notice must not have
# "🔴 <key> FAILED" prepended to it, and a key outside the consequence map must
# not put "(no consequence mapped — add one in send_failure_alert.sh)", a note
# for whoever maintains this script, on the operator's phone.
#
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
#
# When the retry budget runs out anyway the page is NOT dropped: it is written
# to a durable spool (see "Durable spool" below) and re-sent by the next
# successful send or by the 5-minute liveness tick, which calls `--drain`.
set -u

# ── PATH: fixed, and NOT inherited ───────────────────────────────────────────
# The unit that starts this loads EnvironmentFile=, and the instance file there
# carries the OPERATOR's PATH: user-writable directories first (~/.local/bin,
# ~/.npm-global/bin) and no /usr/sbin or /sbin at all. Both halves are bugs for
# something running as root — it must not execute a user-writable binary, and
# dmsetup, cryptsetup, fsck.ext4, smartctl and runuser all live in /usr/sbin,
# where "not found" reaches a script that reads output as an empty ANSWER
# rather than as an error (2026-09-02, scripts/backup-volume-guard.sh).
#
# So the PATH is SET, not extended, and it is the same line in every root
# script. ROBOTHOR_EXTRA_PATH is a TEST-ONLY leading directory, where the suites
# put their stub binaries — it is never set in a unit or in
# /etc/robothor/robothor.env. Anything from the workspace venv is called by
# absolute path (SCRIPT_DIR), never found on PATH.
# See infra/systemd/README.md.
export PATH="${ROBOTHOR_EXTRA_PATH:+$ROBOTHOR_EXTRA_PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ── The tools this script cannot work without ────────────────────────────────
# bash + curl only, by design (see the header) — so curl is the whole
# dependency, and a pager that cannot reach curl must say so rather than
# exit 0 having delivered nothing.
require_tools() {
    local tool missing=0
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "send_failure_alert: required tool not found on PATH: ${tool}" >&2
            missing=1
        fi
    done
    if [ "$missing" = 1 ]; then
        echo "send_failure_alert: PATH=${PATH}" >&2
        exit 1
    fi
}
require_tools curl

MODE="send"
if [[ "${1:-}" == "--drain" ]]; then
    # Drain-only mode: deliver whatever the spool is holding and exit. No unit
    # argument, no cooldown, no new page composed.
    MODE="drain"
    shift
    UNIT="(alert spool drain)"
    BODY=""
else
    UNIT="${1:?usage: send_failure_alert.sh <unit-name> [body] | --drain}"
    BODY="${2:-}"
fi

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
#
# The BODY is checked, not just the key. With two arguments the body IS the
# page and the key is only a dedup label, so a caller that composes its body
# from a path — the backup volume guard names the volume it found unmounted —
# puts the fixture path there and nowhere else. A guard written for the
# one-argument shape never saw it. Both are checked, so neither shape can
# carry a tmpdir to the operator.
if [[ "$UNIT$BODY" == *"pytest-of-"* || "$UNIT$BODY" == *"/pytest-"* ]]; then
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

# ── Durable spool ─────────────────────────────────────────────────────────────
# Since 2026-08-31 the journal carries 63 `curl_rc=6` lines — "Could not
# resolve host: api.telegram.org". The OnFailure path survives those: the
# robothor-alert@ unit has Restart=on-failure behind it, so an exhausted run
# comes back. The callers with NO retrying unit behind them (cron-wrapper.sh,
# backup-offsite.sh, thermal-guard.sh, boot-guard.sh) have nothing to come back
# to — the loop exhausts, the script exits 1, and the page is gone.
#
# A longer backoff only helps the path that already retries. A pinned IP breaks
# on rotation, and curl's --dns-servers needs a c-ares build. So the page is
# written to disk instead and re-sent later: /var/lib (NVMe, NOT tmpfs) so it
# survives the reboot that a boot-window failure usually ends in.
#
# The directory is 1777 sticky (infra/tmpfiles/robothor-restart.conf) because
# root's units and the operator's cron jobs both spool into it and neither can
# chown the other's files; sticky keeps each writer able to delete only its
# own. That does mean any local user can plant a .msg here, so the drain
# applies the same pytest-path refusal the entry guard does.
SPOOL_DIR="${ROBOTHOR_ALERT_SPOOL_DIR:-/var/lib/robothor/alert-spool}"
# Overridable so a test can exercise overflow without writing 51 files.
SPOOL_CAP="${ROBOTHOR_ALERT_SPOOL_CAP:-50}"

# ── A page the endpoint REFUSES must not hold the queue ──────────────────────
# The drain used to break out of its loop on any post failure, with no
# per-file attempt counter and no age-out. A page Telegram permanently rejects
# — a body cut through a multi-byte character, say — therefore sat at the head
# of the spool forever and every page raised after it was never delivered. The
# spool exists to make a page late rather than lost; that inverted it for the
# whole queue.
#
# So a file that cannot go out is moved aside instead of blocking: poison/ is
# a subdirectory (the drain globs *.msg in the top level only, so nothing there
# is retried) and it is kept, not deleted — a page nobody could deliver is
# still evidence.
POISON_DIR="${SPOOL_DIR}/poison"
# Pages dropped over the cap but not yet reported to the operator. Durable,
# because the drop happens mid-outage and the report can only go out once the
# outage ends. Appended to, never overwritten: two drops in the same outage
# are two lines and one total.
DROPPED_NOTE="${SPOOL_DIR}/.dropped"
# ── A queue nothing can move has to have a surface ───────────────────────────
# `--drain` exits 0 whatever happens and drain_spool() returns 0 by design: a
# backlog is not an incident, and failing the liveness unit here would fire an
# OnFailure page about the very outage that filled the spool. The cost was
# that a dead credential, or a day-old queue, produced nothing but journal
# lines on a box nobody reads — the spool promises a page is LATE, not lost,
# and a queue that cannot move breaks that promise in silence.
#
# So a drain that gives up mid-queue leaves ONE line here saying why, and any
# delivered page clears it. scripts/liveness_probe.sh turns a marker that has
# survived 30 minutes into a page on its own key — the sender may itself be
# what is broken, so the last word is that unit's OnFailure=.
STUCK_NOTE="${SPOOL_DIR}/.stuck"
# ~48 ticks of the 5-minute liveness timer ≈ 4h. A page nothing has managed to
# deliver in 4h is not going out; holding the queue open for it costs more than
# it is worth.
SPOOL_MAX_ATTEMPTS="${ROBOTHOR_ALERT_SPOOL_MAX_ATTEMPTS:-48}"
# A day-old page is not an incident report any more.
SPOOL_MAX_AGE="${ROBOTHOR_ALERT_SPOOL_MAX_AGE_SECONDS:-86400}"

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

# The cooldown CHECK itself lives below the spool drain: a unit inside its 1h
# cooldown is exactly the case where pages have been piling up on disk, and an
# early `exit 0` here would leave them there.

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

# The page exactly as the operator would have read it. Composed on demand
# rather than once, because the journal tail sharpens as the failure plays out
# and a backup marker can land while the retry loop waits out the boot window —
# and because the spool needs the same text after the loop has given up.

# ── The journal tail must be valid UTF-8 ─────────────────────────────────────
# The tail is sliced by BYTES (`tail -c`), and the slice lands wherever it
# lands — including the middle of a multi-byte character. Journal lines also
# carry arbitrary bytes of their own: on 2026-09-02 this box's journal put a
# raw 0x80 into a page. Telegram answers a body that is not valid UTF-8 with
# an HTTP 400, and a 400 on a SPOOLED page used to stop the drain at that file
# on every tick forever — one mis-sliced em dash wedged every page behind it.
# (The drain now quarantines a rejected page, but a page that cannot be
# delivered at all is still a page lost.)
#
# So: take a little more than the budget, drop what does not round-trip, cut
# to the real budget, then scrub the NEW boundary too — the second cut can
# split a character just as the first one could.
JOURNAL_TAIL_BYTES="${ROBOTHOR_ALERT_JOURNAL_TAIL_BYTES:-500}"
# Overridable so a test can supply the tail instead of reading this host's
# journal, which differs on every box and every run — and is not guaranteed
# to be decodable at all.
JOURNAL_CMD="${ROBOTHOR_ALERT_JOURNAL_CMD:-journalctl}"
scrub_utf8() {
    if command -v iconv >/dev/null 2>&1; then
        iconv -c -f utf-8 -t utf-8 2>/dev/null || true
    else
        # No iconv (a from-scratch container): fall back to ASCII, which is
        # always valid UTF-8. Losing the em dashes beats losing the page.
        LC_ALL=C tr -d '\200-\377' 2>/dev/null || true
    fi
}

compose_text() {
    # ── With a body, the body IS the message ─────────────────────────────────
    # $1 is the dedup key, not a headline. The sender used to prepend
    # "🔴 <key> FAILED on <host>" and a consequence line to a caller-supplied
    # body, so a RECOVERY notice paged as "🔴 backup-volume-recovered FAILED"
    # above a ✅ line — the pager contradicting the message it was handed —
    # and any key outside the consequence map added "(no consequence mapped —
    # add one in send_failure_alert.sh)", a note for whoever maintains this
    # script, to the operator's phone.
    #
    # Only the trailer survives: a page still has to say when it was raised
    # and which box raised it.
    if [[ -n "$BODY" ]]; then
        printf '%s\n%s' "$BODY" "$(date -Is) on ${HOST}"
        return 0
    fi

    # No body: the page is composed from the unit — headline, consequence, and
    # the tail of that unit's journal.
    local detail journal
    journal="$("$JOURNAL_CMD" -u "$UNIT" -n 5 --no-pager -o cat 2>/dev/null \
        | tail -c "$(( JOURNAL_TAIL_BYTES + 100 ))" \
        | scrub_utf8 \
        | tail -c "$JOURNAL_TAIL_BYTES" \
        | scrub_utf8 || true)"
    detail="Last journal lines:
${journal:-<journal unavailable>}

Check: systemctl status ${UNIT}"
    printf '%s\n%s\n%s\n\n%s' \
        "🔴 ${UNIT} FAILED on ${HOST}" \
        "$(consequence_for "$UNIT")" \
        "$(date -Is)" \
        "$detail"
}

# ── One POST, one verdict ─────────────────────────────────────────────────────
# The HTTP STATUS is checked, not just curl's exit code. curl exits 0 on an
# HTTP 401 -- it fetched the response body just fine, the body simply says
# {"ok":false,"error_code":401}. A revoked bot token or a wrong chat_id
# therefore read as a DELIVERED page: the stamp was touched, arming a 1h
# cooldown on a page nobody received, and the script exited 0 so systemd's
# Restart=on-failure never spent a retry. This is the only paging path for 8
# units, including the engine watchdog and offsite backup.
#
# --retry inside curl covers transient blips within an attempt; the callers'
# loops cover the minutes-long boot-DNS window.
LAST_CURL_RC=0
LAST_HTTP_CODE=""
post_telegram() {
    local text="$1" http_code rc
    http_code=$(curl -sS --max-time 15 --retry 3 --retry-all-errors --retry-delay 2 \
        -o /dev/null -w '%{http_code}' \
        --data-urlencode "chat_id=${ROBOTHOR_TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        "${API_BASE}/bot${ROBOTHOR_TELEGRAM_BOT_TOKEN}/sendMessage" 2>/dev/null)
    rc=$?
    LAST_CURL_RC="$rc"
    LAST_HTTP_CODE="${http_code:-}"
    [ "$rc" -eq 0 ] && [ "${http_code:-0}" -ge 200 ] && [ "${http_code:-0}" -lt 300 ]
}

# ── Spool: an undeliverable page is parked, never dropped ────────────────────
# Written under a .tmp name and renamed, so a drain running concurrently (the
# liveness tick fires every 5 minutes) can never read half a page.
spool_page() {
    local text="$1" file tmp stale
    # The tmpfiles row (infra/tmpfiles/robothor-restart.conf) normally creates
    # this 1777. When it does not exist yet, a bare `mkdir -p` leaves it 0755
    # and owned by whoever spooled first — after which the OTHER account (root's
    # units, or the operator's cron jobs) silently cannot spool at all, and the
    # pages that reached disk would be exactly the ones nobody was missing.
    #
    # Only chmod a directory THIS run created. Widening one somebody else made
    # — to 1777, of all modes — is not the pager's call.
    if [[ ! -d "$SPOOL_DIR" ]] && mkdir -p "$SPOOL_DIR" 2>/dev/null; then
        chmod 1777 "$SPOOL_DIR" 2>/dev/null || true
    fi
    if [[ ! -d "$SPOOL_DIR" || -L "$SPOOL_DIR" || ! -w "$SPOOL_DIR" ]]; then
        echo "send_failure_alert: cannot spool ${UNIT} — ${SPOOL_DIR} is not a writable" \
             "directory (a symlink is refused outright); THIS PAGE IS LOST" >&2
        return 1
    fi
    # One pending page per unit, newest wins.
    #
    # robothor-alert@.service is Restart=on-failure/RestartSec=60 and an
    # exhausted send exits 1, so while DNS is down systemd re-runs the pager
    # about every six minutes. Each run raises the SAME page, and every one of
    # them used to land as its own <epoch>-<key>.<hash>.<pid>.msg — an hour of
    # outage became ten identical files, all of which the drain delivers the
    # moment the network returns. Ten buzzes for one failure is how a real page
    # gets scrolled past, so the pending copy is replaced rather than joined.
    #
    # Only files THIS account owns: root's units and the operator's cron jobs
    # share the spool, and removing the other account's undelivered page would
    # be page loss, not de-duplication.
    shopt -s nullglob
    for stale in "${SPOOL_DIR}/"*"-${SANITIZED}.${UNIT_HASH}."*.msg; do
        [[ -f "$stale" && ! -L "$stale" && -O "$stale" ]] || continue
        rm -f "$stale" "${stale}.attempts" 2>/dev/null || true
    done
    shopt -u nullglob
    # <epoch>-<key>.msg: the epoch prefix is what makes a plain glob sort
    # oldest-first, and $$ keeps two pages raised in the same second apart.
    file="${SPOOL_DIR}/$(date +%s)-${SANITIZED}.${UNIT_HASH}.$$.msg"
    tmp="${file}.tmp"
    if printf '%s\n' "$text" >"$tmp" 2>/dev/null && mv -f "$tmp" "$file" 2>/dev/null; then
        # Deliberately NOT phrased "delivered page for ..." — that string is
        # the delivery announcement, and a log line that reads like one on a
        # page nobody received is the exact confusion this pager keeps
        # relearning (see TestDeliveryIsAnnounced).
        echo "send_failure_alert: page for ${UNIT} was NOT sent; spooled to ${file}" >&2
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || true
    echo "send_failure_alert: could not write ${file}; THIS PAGE IS LOST" >&2
    return 1
}

# Move a page out of the delivery path, keeping it. Never deletes: a page
# nobody could deliver is the evidence of why, and `ls poison/` is where the
# operator goes looking for it.
quarantine_spooled() {
    local f="$1" reason="$2" name
    name="$(basename "$f")"
    # Sticky and world-writable for the same reason the spool is: root's tick
    # and the operator's drain both quarantine into it, and a 0755 dir made by
    # whichever ran first would leave the other unable to move anything here —
    # its rejected pages would stay in the queue instead. Only chmod what this
    # run created.
    if [[ ! -d "$POISON_DIR" ]] && mkdir -p "$POISON_DIR" 2>/dev/null; then
        chmod 1777 "$POISON_DIR" 2>/dev/null || true
    fi
    if [[ -d "$POISON_DIR" ]] && mv -f "$f" "${POISON_DIR}/${name}" 2>/dev/null; then
        rm -f "${f}.attempts" 2>/dev/null || true
        # Loud, and named: a page silently removed from the queue is
        # indistinguishable from a page that was never raised.
        echo "send_failure_alert: QUARANTINED spooled page ${name} to ${POISON_DIR}" \
             "— ${reason}; it will NOT be delivered" >&2
        return 0
    fi
    echo "send_failure_alert: could not quarantine ${name} (${reason});" \
         "it stays in ${SPOOL_DIR} and will be retried" >&2
    return 1
}

# One line, with the time it was raised: this is read by an operator at 3am
# and by the liveness probe, and both want the reason, not a log to grep.
note_stuck() {
    if ! printf '%s %s\n' "$(date -Is)" "$1" >"$STUCK_NOTE" 2>/dev/null; then
        # The spool is 1777 sticky: another account may own the marker. Say so
        # rather than pretending the queue is healthy.
        echo "send_failure_alert: could not write ${STUCK_NOTE} (not owner) —" \
             "a stuck spool will not be visible to the liveness probe" >&2
    fi
}

# Cleared by a DELIVERY, never by an attempt — the marker exists to outlive
# the outage, and a probe that pages about a queue which is moving again is
# exactly the fatigue this pager keeps relearning.
clear_stuck() {
    [[ -e "$STUCK_NOTE" ]] || return 0
    rm -f "$STUCK_NOTE" 2>/dev/null || true
    if [[ -e "$STUCK_NOTE" ]]; then
        echo "send_failure_alert: could not clear ${STUCK_NOTE} (not owner) —" \
             "the stuck-spool page will repeat" >&2
    fi
}

# How many times this exact file has already failed to go out. The counter is
# a sidecar rather than a rewrite of the page, so the bytes the operator will
# eventually read are never touched by bookkeeping.
attempt_count() {
    local n
    n="$(head -n 1 "${1}.attempts" 2>/dev/null || true)"
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    printf '%s' "$n"
}

# Deliver what the spool is holding, oldest first. Called by `--drain` (the
# liveness timer, every 5 minutes) and at the start of every normal send.
#
# Never fails its caller: a drain that cannot run yet is not an incident, and
# a page still on disk is not a lost page.
drain_spool() {
    [[ -d "$SPOOL_DIR" && ! -L "$SPOOL_DIR" ]] || return 0
    local files=()
    shopt -s nullglob
    files=("${SPOOL_DIR}"/*.msg)
    shopt -u nullglob
    if (( ${#files[@]} == 0 )); then
        # Nothing left to deliver: the last page may have left the queue by
        # being quarantined rather than delivered, and a marker with nothing
        # behind it pages about nothing.
        clear_stuck
        return 0
    fi

    # ── Drain only what this account can actually delete ─────────────────────
    # The spool is 1777 sticky, so root's units and the operator's cron jobs
    # both write here and NEITHER can unlink the other's files. A non-root
    # drain that picks up a root-owned page delivers it, fails to remove it,
    # and delivers it again five minutes later — forever, while the log
    # reports a clean delivery each time. Root drains everything; everyone
    # else leaves other people's pages to root's own tick.
    local f keep=() foreign=0
    if (( EUID != 0 )); then
        for f in "${files[@]}"; do
            if [[ -O "$f" ]]; then
                keep+=("$f")
            else
                foreign=$(( foreign + 1 ))
            fi
        done
        if (( foreign )); then
            echo "send_failure_alert: leaving ${foreign} spool file(s) owned by another" \
                 "account to root's drain — this user could not delete them after sending" >&2
        fi
        files=("${keep[@]}")
        (( ${#files[@]} )) || return 0
    fi

    source_secrets
    if [[ -z "${ROBOTHOR_TELEGRAM_BOT_TOKEN:-}" || -z "${ROBOTHOR_TELEGRAM_CHAT_ID:-}" ]]; then
        echo "send_failure_alert: ${#files[@]} page(s) spooled in ${SPOOL_DIR}, but there are" \
             "no credentials yet to drain them" >&2
        return 0
    fi

    # Pages this spool has already thrown away and not yet owned up to. The
    # count is durable (a file in the spool, appended to) because the drop and
    # the chance to report it are separated by the whole outage.
    local pending_dropped=0 line
    if [[ -r "$DROPPED_NOTE" ]]; then
        while read -r line || [[ -n "$line" ]]; do
            [[ "$line" =~ ^[0-9]+$ ]] && pending_dropped=$(( pending_dropped + line ))
        done < "$DROPPED_NOTE"
    fi

    # An unbounded spool is its own outage: a week of DNS loss would dump
    # hundreds of stale pages the moment the network returned. Keep the newest
    # SPOOL_CAP and say out loud how many were dropped — a silent truncation
    # would be the pager lying about what it had.
    local dropped=0 i over=0
    if (( ${#files[@]} > SPOOL_CAP )); then
        over=$(( ${#files[@]} - SPOOL_CAP ))
        # The `rm` is CHECKED. `rm -f ... || true` on a file this account does
        # not own removed nothing while the pager announced N pages dropped —
        # a truncation notice for a truncation that never happened, and the
        # same files back on the next tick.
        keep=()
        for (( i = 0; i < ${#files[@]}; i++ )); do
            if (( i < over )); then
                if rm -f "${files[i]}" 2>/dev/null && [[ ! -e "${files[i]}" ]]; then
                    rm -f "${files[i]}.attempts" 2>/dev/null || true
                    dropped=$(( dropped + 1 ))
                    continue
                fi
                echo "send_failure_alert: cannot remove over-cap spool file ${files[i]}" \
                     "(not owner) — keeping it" >&2
            fi
            keep+=("${files[i]}")
        done
        files=("${keep[@]}")
    fi
    if (( dropped )); then
        echo "send_failure_alert: alert spool over the ${SPOOL_CAP}-page cap —" \
             "${dropped} older pages dropped" >&2
        # The notice used to be POSTED right here, and that is the one moment
        # it cannot go out: the spool only overflows because nothing has been
        # deliverable for hours. So the operator was never told that N pages
        # had been thrown away — the notice died in the stderr of a cron job
        # nobody reads. Record it instead and let the next page that actually
        # lands carry it.
        printf '%s\n' "$dropped" >>"$DROPPED_NOTE" 2>/dev/null || true
        pending_dropped=$(( pending_dropped + dropped ))
    fi
    local drop_notice=""
    if (( pending_dropped )); then
        drop_notice="⏳ ${pending_dropped} older pages dropped from the alert spool on ${HOST} (over the ${SPOOL_CAP}-page cap)
"
    fi

    # `left` is what is still WAITING, not the size of the queue this drain
    # started with: a page quarantined on the way through has left the
    # delivery path for good, and counting it as spooled overstates the
    # backlog by exactly the pages nothing will ever retry.
    local f base epoch queued text delivered=0 now attempts code
    local left=${#files[@]} head_age=0
    now="$(date +%s)"
    for f in "${files[@]}"; do
        base="$(basename "$f")"
        epoch="${base%%-*}"
        queued="??:??"
        [[ "$epoch" =~ ^[0-9]+$ ]] && queued="$(date -d "@${epoch}" +%H:%M 2>/dev/null || echo '??:??')"

        # ── Age-out ──────────────────────────────────────────────────────────
        # Checked BEFORE the file is read, so a page that cannot be read at all
        # still leaves the queue eventually.
        if [[ "$epoch" =~ ^[0-9]+$ ]] && (( now - epoch > SPOOL_MAX_AGE )); then
            if quarantine_spooled "$f" "queued $(( (now - epoch) / 3600 ))h ago, past the ${SPOOL_MAX_AGE}s age cap"; then
                left=$(( left - 1 ))
            fi
            continue
        fi
        # ── Attempt budget ───────────────────────────────────────────────────
        attempts="$(attempt_count "$f")"
        if (( attempts >= SPOOL_MAX_ATTEMPTS )); then
            if quarantine_spooled "$f" "${attempts} failed delivery attempts, at the ${SPOOL_MAX_ATTEMPTS}-attempt budget"; then
                left=$(( left - 1 ))
            fi
            continue
        fi

        text="$(cat "$f" 2>/dev/null || true)"
        if [[ -z "$text" ]]; then
            # NOT deleted: an unreadable page is one whose contents nobody has
            # seen, and deleting it makes the loss unexaminable. Named, because
            # a file skipped in silence is a page that quietly never arrives.
            # The age cap above is what eventually clears it.
            echo "send_failure_alert: spooled page ${base} is empty or unreadable —" \
                 "skipping it (kept for inspection; it ages out into ${POISON_DIR})" >&2
            continue
        fi
        # The spool dir is world-writable by design (1777), and the entry guard
        # above only ever saw the unit NAME. Re-apply it to the spooled text:
        # a suite run that spooled a fixture page must not reach the operator
        # five minutes later through this door.
        if [[ "$text" == *"pytest-of-"* || "$text" == *"/pytest-"* ]]; then
            echo "send_failure_alert: dropping spooled page that names a pytest temp path:" \
                 "${base}" >&2
            rm -f "$f" 2>/dev/null || true
            rm -f "${f}.attempts" 2>/dev/null || true
            [[ -e "$f" ]] || left=$(( left - 1 ))
            continue
        fi
        # Say it is late and say when it was raised: a page whose timestamp is
        # an hour old reads as a live incident unless the delay is on the face
        # of it.
        # A pending truncation notice rides the first page that actually
        # lands, rather than being posted at the moment of the drop — which is
        # the moment nothing can be posted at all.
        if post_telegram "${drop_notice}⏳ DELAYED (queued ${queued}):
${text}"; then
            # The unlink is CHECKED, and its failure stops the drain. In the
            # 1777 sticky spool this account cannot delete a file it does not
            # own: `rm -f ... || true` then counted a delivery, so the same
            # page went out again on every drain while the log announced a
            # clean delivery each time. Nothing after this file is counted —
            # every one of them would be a page the operator gets twice.
            if ! rm -f "$f" 2>/dev/null || [[ -e "$f" ]]; then
                echo "send_failure_alert: cannot remove delivered spool file ${f}" \
                     "(not owner) — stopping drain to avoid re-sending" >&2
                break
            fi
            rm -f "${f}.attempts" 2>/dev/null || true
            delivered=$(( delivered + 1 ))
            clear_stuck
            if [[ -n "$drop_notice" ]]; then
                # Told once. Clearing only AFTER a delivered page is the whole
                # point: a counter cleared on the attempt would lose the notice
                # in exactly the outage that produced it.
                drop_notice=""
                rm -f "$DROPPED_NOTE" 2>/dev/null || true
                if [[ -e "$DROPPED_NOTE" ]]; then
                    echo "send_failure_alert: could not clear ${DROPPED_NOTE} (not owner)" \
                         "— the spool-truncation notice will repeat" >&2
                fi
            fi
            continue
        fi

        # ── Refusal vs unavailability ────────────────────────────────────────
        # Telegram answering 400 (or 413/414) is a verdict on THIS message:
        # retrying it is retrying the same bytes against the same rule, and
        # while it sits at the head of the queue nothing behind it moves. Take
        # it out of the path and CONTINUE.
        #
        # Deliberately NOT every 4xx. 401/403 mean the token is wrong and 429
        # means slow down — both answer the same way to every page in the
        # spool, so quarantining on them would empty the whole queue into
        # poison/ over one bad credential. Those are availability problems and
        # take the 5xx path.
        code="${LAST_HTTP_CODE:-}"
        if [[ "$code" =~ ^4[0-9][0-9]$ ]] && [[ ! "$code" =~ ^(401|403|408|429)$ ]]; then
            if quarantine_spooled "$f" "Telegram refused it with HTTP ${code} (content rejection, not an outage)"; then
                left=$(( left - 1 ))
            fi
            continue
        fi

        # Availability: the message is fine, the endpoint is not. Count the
        # attempt against this file and stop — burning the rest of the spool
        # against a dead endpoint delivers nothing and loses the ordering.
        attempts=$(( attempts + 1 ))
        printf '%s\n' "$attempts" >"${f}.attempts" 2>/dev/null || true
        # ── Is this a blip, or is the queue stuck? ───────────────────────────
        # Half the budget spent on the head of the queue, or a head page that
        # has been waiting half the age cap, is no longer an outage anyone is
        # waiting out: it is a credential to rotate or a page to read out of
        # poison/. Below that, this is a DNS blip and the next tick handles
        # it — marking it would train the operator to ignore the marker.
        head_age=0
        [[ "$epoch" =~ ^[0-9]+$ ]] && head_age=$(( now - epoch ))
        if (( attempts >= (SPOOL_MAX_ATTEMPTS + 1) / 2 )); then
            note_stuck "${base} undelivered after ${attempts}/${SPOOL_MAX_ATTEMPTS} attempts (curl_rc=${LAST_CURL_RC} http_status=${LAST_HTTP_CODE:-none})"
        elif (( head_age > SPOOL_MAX_AGE / 2 )); then
            note_stuck "${base} queued $(( head_age / 3600 ))h ago and still undelivered (curl_rc=${LAST_CURL_RC} http_status=${LAST_HTTP_CODE:-none})"
        fi
        echo "send_failure_alert: spool drain stopped at ${base}" \
             "(curl_rc=${LAST_CURL_RC} http_status=${LAST_HTTP_CODE:-none};" \
             "attempt ${attempts}/${SPOOL_MAX_ATTEMPTS});" \
             "$(( left - delivered )) page(s) still spooled" >&2
        break
    done
    if (( delivered )); then
        echo "send_failure_alert: drained ${delivered} spooled page(s) from ${SPOOL_DIR}"
    fi
    return 0
}

# Every invocation drains first — the sender is the only process that knows how
# to deliver a page, so every time it runs is a chance to clear the backlog.
drain_spool
if [[ "$MODE" == "drain" ]]; then
    exit 0
fi

# ── Cooldown check ────────────────────────────────────────────────────────────
# Below the drain deliberately (see STAMP_FILE above).
if [[ -n "$STAMP_FILE" && -f "$STAMP_FILE" ]]; then
    NOW=$(date +%s)
    STAMP_TIME=$(stat -c %Y "$STAMP_FILE" 2>/dev/null || echo 0)
    AGE=$(( NOW - STAMP_TIME ))
    if (( AGE < COOLDOWN )); then
        echo "send_failure_alert: suppressed duplicate page for ${UNIT} (${AGE}s < ${COOLDOWN}s)"
        exit 0
    fi
fi

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
    TEXT="$(compose_text)"

    if post_telegram "$TEXT"; then
        http_code="$LAST_HTTP_CODE"
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
         "(curl_rc=${LAST_CURL_RC} http_status=${LAST_HTTP_CODE:-none})" >&2
done

echo "send_failure_alert: failed to send Telegram message after ${MAX_ATTEMPTS} attempts" >&2
# The retry budget is spent, but the page is not lost: park it for the next
# send or the next liveness tick. Composed here rather than reused from the
# loop because the loop may never have reached the compose step at all — a
# boot with no decrypted secrets yet `continue`s before it.
spool_page "$(compose_text)" || true
# Still a failure: the caller's unit must go `failed` so systemd's own
# Restart=/OnFailure= plumbing sees it, exactly as before.
exit 1
