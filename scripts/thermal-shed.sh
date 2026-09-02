#!/bin/bash
# REPO MIRROR of /usr/local/bin/robothor-thermal-shed.sh — installed by
# scripts/install-host-scripts.sh, drift-checked daily by guardrail_watch.
#
# The SHEDDING half of thermal protection. Its peer, robothor-thermal-guard.sh,
# caps CPU frequency, pages, and clean-reboots at 94C. This one stops workload so
# that reboot is never reached.
#
# It ran for months on the first instance with NO repo copy at all — never
# committed on any branch — so a rebuilt box would silently have lost the guard
# that saves work while keeping the one that reboots. Adopted 2026-08-28.
#
# Why these rungs (docs/runbooks/THERMAL.md, measured 2026-08-28):
#   The package budget is shared by CPU and GPU. One 27B stream alone plateaus
#   ~85C, but that stream PLUS a saturated CPU reached 96C on 2026-08-28. So the
#   rungs cannot be set from the GPU-only peak; they must leave room for the CPU
#   term as well:
#       ~85C   one 27B stream, CPU idle
#       82C    stage 1 — stop vision (sustained 6s)
#       86C    stage 2 — stop webcam, unload models (immediate)
#       90C    peer guard pages
#       94C    peer guard clean-reboots
#   The previous values (90C sustained 60s / 92C) could not win that race: at the
#   measured ramp of ~2C/s the box passed 94C before the 60s sustain elapsed, so
#   the machine always rebooted instead of shedding. That is the loop of
#   2026-08-28.
#
# Journal tag: thermal-shed. State: /run/thermal-guard/stage (absent = stage 0).

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
# The shed IS `systemctl stop`. Without it this loop reads temperatures
# forever and shuts nothing down, which is the inert control its peer
# guard's 94C reboot then covers for — badly.
require_tools() {
    local tool missing=0
    for tool in "$@"; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "thermal-shed: required tool not found on PATH: ${tool}" >&2
            missing=1
        fi
    done
    if [ "$missing" = 1 ]; then
        echo "thermal-shed: PATH=${PATH}" >&2
        exit 1
    fi
}
require_tools cat date logger systemctl sleep

STAGE1_C=${ROBOTHOR_SHED_STAGE1_C:-82}       # stop robothor-vision
STAGE2_C=${ROBOTHOR_SHED_STAGE2_C:-86}       # + stop webcam encode, unload LLM models
                                         # (robothor-thermal-guard reboots at 94C; EC hard-cuts ~95C — fire with margin below both)
RECOVER_C=${ROBOTHOR_SHED_RECOVER_C:-78}    # temps must fall to here to recover
STAGE1_SUSTAIN=${ROBOTHOR_SHED_STAGE1_SUSTAIN:-6}  # seconds above STAGE1_C before acting
RECOVER_SUSTAIN=${RECOVER_SUSTAIN:-300}  # seconds below RECOVER_C before restoring
MIN_SHED=${MIN_SHED:-600}                # minimum seconds to stay shed (anti-flap)
# 2C/s under load means a 10s poll can miss 20C. On 2026-08-28 this guard read
# 85C, then 96C on its next sample — it fired stage 2 correctly and one second
# too late, because the peer guard's reboot had already been decided. Sampling
# sysfs is free; the interval was the whole failure.
POLL=${ROBOTHOR_SHED_POLL:-2}

# Max across ALL zones — on 2026-08-19 watching only TSOC/TGPU lost a race to a guard reading every zone.
ZONES="/sys/class/thermal/thermal_zone*/temp"

STATE_DIR=/run/thermal-guard
mkdir -p "$STATE_DIR"

stage=0
above_since=0
below_since=0
shed_at=0
last_report=0

# Adopt persisted stage so a guard restart never orphans shed services
# (a restart on 2026-08-19 forgot a stage-2 shed and left vision stopped).
if [ -f "$STATE_DIR/stage" ]; then
    stage=$(cat "$STATE_DIR/stage" 2>/dev/null || echo 0)
    case "$stage" in 1|2) shed_at=$(date +%s);; *) stage=0;; esac
    [ "$stage" -gt 0 ] && logger -t thermal-shed "resumed at stage ${stage} from state file"
fi

max_temp() {
    local m=0 t z
    for z in $ZONES; do
        t=$(cat "$z" 2>/dev/null) || continue
        t=$((t / 1000))
        [ "$t" -gt "$m" ] && m=$t
    done
    echo "$m"
}

notify() {
    # Best-effort event into the memory pipeline so the triage/supervisor
    # agents surface it. Must never block or fail the guard.
    curl -s -m 5 -X POST http://localhost:9099/ingest \
        -H "Content-Type: application/json" \
        -d "{\"content\": \"$1\", \"source_channel\": \"camera\", \"content_type\": \"event\", \"metadata\": {\"detection_type\": \"thermal\", \"importance_score\": 0.9}}" \
        >/dev/null 2>&1 || true
}

unload_models() {
    local names n
    names=$(curl -s -m 5 http://127.0.0.1:11434/api/ps 2>/dev/null |
        python3 -c "import sys,json; [print(m['name']) for m in json.load(sys.stdin).get('models',[])]" 2>/dev/null)
    for n in $names; do
        curl -s -m 30 -X POST http://127.0.0.1:11434/api/generate \
            -d "{\"model\": \"$n\", \"keep_alive\": 0}" >/dev/null 2>&1 || true
    done
}

logger -t thermal-shed "started (stage1=${STAGE1_C}C stage2=${STAGE2_C}C recover=${RECOVER_C}C)"

while true; do
    now=$(date +%s)
    temp=$(max_temp)

    if [ $((now - last_report)) -ge 300 ]; then
        logger -t thermal-shed "temp=${temp}C stage=${stage}"
        last_report=$now
    fi

    if [ "$stage" -lt 2 ] && [ "$temp" -ge "$STAGE2_C" ]; then
        logger -t thermal-shed "CRITICAL: ${temp}C >= ${STAGE2_C}C — stage 2 shed (vision, webcam, LLM models)"
        systemctl stop robothor-vision || true
        systemctl stop mediamtx-webcam || true
        unload_models
        stage=2; shed_at=$now; below_since=0
        echo 2 > "$STATE_DIR/stage"
        notify "Thermal guard stage 2 at ${temp}C: stopped vision and webcam services, unloaded LLM models to prevent thermal shutdown"
    elif [ "$stage" -eq 0 ] && [ "$temp" -ge "$STAGE1_C" ]; then
        [ "$above_since" -eq 0 ] && above_since=$now
        if [ $((now - above_since)) -ge "$STAGE1_SUSTAIN" ]; then
            logger -t thermal-shed "WARNING: ${temp}C >= ${STAGE1_C}C sustained ${STAGE1_SUSTAIN}s — stage 1 shed (vision)"
            systemctl stop robothor-vision || true
            stage=1; shed_at=$now; below_since=0
            echo 1 > "$STATE_DIR/stage"
            notify "Thermal guard stage 1 at ${temp}C: stopped vision service to prevent thermal shutdown"
        fi
    elif [ "$temp" -lt "$STAGE1_C" ]; then
        above_since=0
    fi

    if [ "$stage" -gt 0 ]; then
        if [ "$temp" -le "$RECOVER_C" ]; then
            [ "$below_since" -eq 0 ] && below_since=$now
            if [ $((now - below_since)) -ge "$RECOVER_SUSTAIN" ] && [ $((now - shed_at)) -ge "$MIN_SHED" ]; then
                logger -t thermal-shed "recovered at ${temp}C — restarting services (was stage ${stage})"
                systemctl start mediamtx-webcam || true
                systemctl start robothor-vision || true
                stage=0; above_since=0; below_since=0
                rm -f "$STATE_DIR/stage"
                notify "Thermal guard recovered at ${temp}C: services restarted"
            fi
        else
            below_since=0
        fi
    fi

    sleep "$POLL"
done
