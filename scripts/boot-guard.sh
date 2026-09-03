#!/usr/bin/env bash
# REPO MIRROR of /usr/local/bin/robothor-boot-guard.sh — installed by
# scripts/install-host-scripts.sh, drift-checked daily by guardrail_watch.
#
# Crash-loop breaker. This box is administered REMOTELY ONLY: if it reboots in a
# loop, the window to get a shell before the next reboot is the whole recovery
# budget. On 2026-08-28 that window was ~2 minutes, three times in a row, because
# every boot restarted the inference workload that overheated it.
#
# So: count boots. If the box has booted repeatedly in a short window, something
# is wrong that starting inference again will not fix — hold inference down and
# leave the machine up, idle and reachable. Normal boots are unaffected.
#
# The inhibit lives in /run (tmpfs) so it is re-decided every boot rather than
# latching forever; the boot history lives in /var/lib so it survives one.
set -uo pipefail

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
# The boot count comes out of awk and wc. A count that is empty because a
# tool was not found reads as zero boots — the crash loop this exists to
# break would then never be detected.
require_tools() {
    local tool missing=0
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "boot-guard: required tool not found on PATH: ${tool}" >&2
            missing=1
        fi
    done
    if [ "$missing" = 1 ]; then
        echo "boot-guard: PATH=${PATH}" >&2
        exit 1
    fi
}
require_tools date mktemp awk wc tr

HIST=${ROBOTHOR_BOOT_HISTORY:-/var/lib/robothor/boot-history}
INHIBIT=${ROBOTHOR_INHIBIT_FLAG:-/run/robothor/INHIBIT_INFERENCE}
WINDOW=${ROBOTHOR_BOOT_LOOP_WINDOW:-900}   # 15 minutes
LIMIT=${ROBOTHOR_BOOT_LOOP_LIMIT:-3}       # boots within the window

now=$(date +%s)
mkdir -p "$(dirname "$HIST")" "$(dirname "$INHIBIT")"
echo "$now" >> "$HIST"

# Keep only boots inside the window, so a healthy box never accumulates.
tmp=$(mktemp)
awk -v now="$now" -v w="$WINDOW" '$1 ~ /^[0-9]+$/ && $1 > now - w' "$HIST" > "$tmp" 2>/dev/null
mv "$tmp" "$HIST"
count=$(wc -l < "$HIST" | tr -d ' ')

if [ "${count:-0}" -ge "$LIMIT" ]; then
    printf 'boot-loop detected: %s boots in %ss — inhibiting inference\n' "$count" "$WINDOW"
    cat > "$INHIBIT" <<MSG
Inference inhibited by robothor-boot-guard at $(date -Is).
$count boots within ${WINDOW}s looks like a crash loop.

The box is deliberately left idle and reachable. To investigate:
    journalctl -b -1 -u robothor-thermal-guard -u robothor-engine
    cat /var/lib/robothor/boot-history

To clear once the cause is fixed:
    sudo rm -f $INHIBIT $HIST
    sudo systemctl start ollama robothor-engine
MSG
    "${ROBOTHOR_WORKSPACE:-/opt/robothor}/scripts/send_failure_alert.sh" \
        "BOOT-LOOP $count boots in ${WINDOW}s — inference inhibited, box left idle" || true
    exit 0
fi

printf 'boot ok: %s boot(s) in the last %ss\n' "$count" "$WINDOW"
exit 0
