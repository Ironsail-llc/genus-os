#!/usr/bin/env bash
# REPO MIRROR of /usr/local/bin/robothor-thermal-guard.sh — installed by
# scripts/install-host-scripts.sh, drift-checked daily by guardrail_watch.
# This is a SAFETY control (born from the Aug 2026 GPU thermal event); it ran
# for weeks with no repo copy at all, meaning a rebuilt box would silently
# lose it. The sender path resolves via ROBOTHOR_WORKSPACE from
# /etc/robothor/robothor.env, which the unit sources.
# Thermal guard: the firmware exposes no trip points to the OS ("[Firmware
# Bug]: No valid trip points!"), so the box hard-cuts power with zero warning
# near ~95C — three times in Aug 2026, each mistaken for a power outage.
# This guard restores the warning layer the firmware doesn't provide:
#   >= THROTTLE_C : drop the CPU freq cap to THROTTLE_PCT (self-protective,
#                   no page) until back under RESTORE_C
#   >= WARN_C     : page the operator (rate-limited via the alert cooldown dir)
#   >= CRIT_C     : clean reboot — DB-safe like a poweroff, but self-recovering
#                   the firmware's alternative is an instant dirty cut.
set -euo pipefail

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
# This one reboots the box at 94C. systemctl is how; cat is how it reads
# the temperature at all; stat and date are the page cooldown. A thermal
# guard that cannot act must fail its unit, not run on reporting nothing.
require_tools() {
    local tool missing=0
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "thermal-guard: required tool not found on PATH: ${tool}" >&2
            missing=1
        fi
    done
    if [ "$missing" = 1 ]; then
        echo "thermal-guard: PATH=${PATH}" >&2
        exit 1
    fi
}
require_tools cat date stat systemctl

WARN_C=${ROBOTHOR_THERMAL_WARN_C:-90}
CRIT_C=${ROBOTHOR_THERMAL_CRIT_C:-94}
THROTTLE_C=${ROBOTHOR_THERMAL_THROTTLE_C:-85}
RESTORE_C=${ROBOTHOR_THERMAL_RESTORE_C:-75}
NORMAL_PCT=${ROBOTHOR_THERMAL_NORMAL_PCT:-65}
THROTTLE_PCT=${ROBOTHOR_THERMAL_THROTTLE_PCT:-50}
STATE_DIR=${ROBOTHOR_ALERT_STATE_DIR:-/run/robothor/alert-cooldown}
mkdir -p "$STATE_DIR"

set_cap() { # $1 = percent
    local maxf cap
    maxf=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)
    cap=$((maxf * $1 / 100))
    for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do
        echo "$cap" > "$c"
    done
}

max=0
for z in /sys/class/thermal/thermal_zone*/temp; do
    t=$(cat "$z" 2>/dev/null || echo 0)
    [ "$t" -gt "$max" ] && max=$t
done
max_c=$((max / 1000))
throttled="$STATE_DIR/thermal-throttled.flag"

if [ "$max_c" -ge "$CRIT_C" ]; then
    echo "CRITICAL: ${max_c}C >= ${CRIT_C}C — initiating CLEAN reboot before the firmware hard-cuts"
    "${ROBOTHOR_WORKSPACE:-/opt/robothor}/scripts/send_failure_alert.sh" "THERMAL-CRITICAL ${max_c}C — clean reboot now" || true
    systemctl reboot
elif [ "$max_c" -ge "$WARN_C" ]; then
    stamp="$STATE_DIR/thermal-warn.stamp"
    now=$(date +%s)
    last=$(stat -c %Y "$stamp" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge 1800 ]; then
        echo "WARN: ${max_c}C >= ${WARN_C}C — paging operator"
        "${ROBOTHOR_WORKSPACE:-/opt/robothor}/scripts/send_failure_alert.sh" "THERMAL-WARN ${max_c}C (firmware hard-cuts ~95C)" || true
        touch "$stamp"
    else
        echo "WARN: ${max_c}C (page suppressed, cooldown)"
    fi
    set_cap "$THROTTLE_PCT"; touch "$throttled"
elif [ "$max_c" -ge "$THROTTLE_C" ]; then
    if [ ! -e "$throttled" ]; then
        echo "THROTTLE: ${max_c}C >= ${THROTTLE_C}C — CPU cap ${NORMAL_PCT}% -> ${THROTTLE_PCT}%"
        set_cap "$THROTTLE_PCT"; touch "$throttled"
    else
        echo "THROTTLE holding: ${max_c}C"
    fi
elif [ -e "$throttled" ] && [ "$max_c" -le "$RESTORE_C" ]; then
    echo "RESTORE: ${max_c}C <= ${RESTORE_C}C — CPU cap back to ${NORMAL_PCT}%"
    set_cap "$NORMAL_PCT"; rm -f "$throttled"
else
    echo "ok: ${max_c}C"
fi
