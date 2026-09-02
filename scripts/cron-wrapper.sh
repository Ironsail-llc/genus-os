#!/bin/bash
# Cron wrapper: ensure secrets are available, run the command, and page the
# operator if it fails.
# Usage: cron-wrapper.sh <command> [args...]
#
# Sources /run/robothor/secrets.env (decrypted by systemd or previous cron).
# If the file doesn't exist yet (e.g., after a reboot before services start),
# runs decrypt-secrets.sh to create it.
#
# On a non-zero exit of the wrapped command the wrapper pages via
# send_failure_alert.sh (same Telegram path as the systemd OnFailure hook,
# including its per-command cooldown dedup). Cron mails root and nothing reads
# root's mail — a crontab entry pointing at a deleted script was failing every
# 30 minutes for hours with no page before this existed. Fail-open: a broken
# pager must never change the cron job's own exit semantics.

set -uo pipefail

# ── PATH: fixed, and NOT inherited ───────────────────────────────────────────
# cron hands a job /usr/bin:/bin and nothing else, and this wrapper SOURCES
# /etc/robothor/robothor.env below — the instance file that
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
# Remembered, because the instance env sourced below sets PATH too — see the
# restore after that source.
ROBOTHOR_FIXED_PATH="$PATH"

# ── The tools this script cannot work without ────────────────────────────────
# cron gives a script /usr/bin:/bin and nothing else, which is why this
# wrapper sets the PATH its wrapped command inherits.
require_tools() {
    local tool missing=0
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "cron-wrapper: required tool not found on PATH: ${tool}" >&2
            missing=1
        fi
    done
    if [ "$missing" = 1 ]; then
        echo "cron-wrapper: PATH=${PATH}" >&2
        exit 1
    fi
}
require_tools dirname

SECRETS_ENV="${ROBOTHOR_SECRETS_FILE:-/run/robothor/secrets.env}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DECRYPT_SCRIPT="${SCRIPT_DIR}/decrypt-secrets.sh"

if [ ! -f "$SECRETS_ENV" ]; then
    "$DECRYPT_SCRIPT" 2>/dev/null || true
fi

if [ -f "$SECRETS_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
    set +a
fi

# Source instance config (sets ROBOTHOR_DB_USER, ROBOTHOR_OWNER_NAME, etc.)
INSTANCE_ENV="${ROBOTHOR_INSTANCE_ENV:-/etc/robothor/robothor.env}"
if [ -f "$INSTANCE_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$INSTANCE_ENV"
    set +a
fi

# …and PATH is one of the things that file sets. `set -a` above exported it
# over the fixed value, so every command this wrapper runs — including the
# cron job itself — would have inherited the operator's user-writable
# directories, as root, which is the whole thing the prelude prevents. The
# fixed value — exactly what the line at the top produced — is put back.
PATH="$ROBOTHOR_FIXED_PATH"
export PATH

# Cron does not set USER — set it so robothor.config uses the correct DB user.
# pg_hba.conf uses peer auth on Unix sockets, requiring OS user = PG role.
export USER="${USER:-robothor}"
export ROBOTHOR_DB_USER="${ROBOTHOR_DB_USER:-robothor}"

"$@"
rc=$?

if [ "$rc" -ne 0 ]; then
    # Pseudo-unit name carries the command and exit code; truncated so a long
    # argv cannot balloon the page. journalctl finds nothing for it, which the
    # pager already handles ("<journal unavailable>").
    desc="cron: $* (exit ${rc})"
    desc="${desc:0:200}"
    ALERT="${SCRIPT_DIR}/send_failure_alert.sh"
    if [ -x "$ALERT" ]; then
        # A tight retry budget: paging is best-effort here and must not stall
        # the cron slot for the pager's full 5-minute boot-window budget.
        ROBOTHOR_ALERT_MAX_ATTEMPTS="${ROBOTHOR_CRON_ALERT_MAX_ATTEMPTS:-2}" \
        ROBOTHOR_ALERT_RETRY_DELAY="${ROBOTHOR_CRON_ALERT_RETRY_DELAY:-15}" \
            "$ALERT" "$desc" || true
    fi
fi

exit "$rc"
