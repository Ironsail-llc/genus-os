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
