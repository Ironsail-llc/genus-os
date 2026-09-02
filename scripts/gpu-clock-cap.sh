#!/usr/bin/env bash
# REPO MIRROR of /usr/local/bin/robothor-gpu-clock-cap.sh — installed by
# scripts/install-host-scripts.sh, drift-checked daily by guardrail_watch.
#
# Cap the GPU's maximum SM clock. This is the only HARDWARE bound available on
# GB10: the part reports no power limit at all (`nvidia-smi -q -d POWER` returns
# N/A for every limit field), so clock is the one lever that bounds watts, and
# watts are what the package temperature tracks.
#
# Measured 2026-08-28 (docs/runbooks/THERMAL.md), qwen3.8:27b, six back-to-back
# 7.5k-token requests:
#     uncapped (3003 MHz) : 86C peak, 69W, 26.6 tok/s generation
#     2000 MHz            : 81C sustained, 39W, 28.3 tok/s
#     1500 MHz            : 76C sustained, 28W, 24.0 tok/s
#
# Note the uncapped row is SLOWER than 2000 MHz while running 11C hotter — above
# ~2000 MHz this part burns watts fighting its own thermal wall. Capping is not a
# throughput sacrifice so much as a correction.
#
# 1500 MHz is chosen because this instance is administered remotely: 76C leaves
# 18C to the peer guard's 94C clean reboot and 6C below thermal-shed's stage 1,
# for ~10% less generation throughput than uncapped. Raise it only with new
# measurements, and never above 2000 MHz without them.
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

CAP_MHZ=${ROBOTHOR_GPU_CLOCK_CAP_MHZ:-1500}
MIN_MHZ=${ROBOTHOR_GPU_CLOCK_MIN_MHZ:-300}

command -v nvidia-smi >/dev/null 2>&1 || { echo "no nvidia-smi — nothing to cap"; exit 0; }

# The driver may not be ready the instant this unit runs.
for attempt in 1 2 3 4 5 6; do
    if nvidia-smi -lgc "${MIN_MHZ},${CAP_MHZ}" 2>&1; then
        echo "GPU clock capped to ${MIN_MHZ}-${CAP_MHZ} MHz"
        exit 0
    fi
    echo "attempt ${attempt}: driver not ready, retrying"
    sleep 5
done

echo "FAILED to cap GPU clock after 6 attempts" >&2
exit 1
