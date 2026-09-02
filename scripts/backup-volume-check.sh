#!/usr/bin/env bash
# Is the backup volume actually usable? — the probe that touches the disk.
#
# WHY THIS EXISTS
#   The encrypted USB backup volume goes `emergency_ro` when the drive drops
#   off the bus (2026-07-14, 2026-08-27). In that state the kernel keeps
#   answering stat(): the mountpoint is still a mountpoint, the directory is
#   still a directory, `df` still reports the cached capacity. Only readdir()
#   and write() fail.
#
#   Every guard the backup chain had was a stat() guard — `mountpoint -q` in
#   backup-ssd.sh and pg-basebackup.sh, `[[ -d ]]` in wal-offsite.sh — so all
#   of them PASSED and the units ran, wrote nothing, and failed.
#   robothor-wal-offsite runs every 15 minutes: 96 OnFailure triggers a day,
#   ~22 Telegram pages whose entire content was a unit name. A page that fires
#   96 times for one unfixed condition is a muted pager.
#
# HOW IT IS USED
#   As systemd `ExecCondition=` on the backup units. systemd 255 semantics:
#
#     exit 0        the condition holds — the unit runs
#     exit 1-254    the condition does not hold — the unit is SKIPPED
#                   (Result=exec-condition, OnFailure does NOT fire, no page)
#     exit 255      the condition check itself FAILED — the unit fails and
#                   OnFailure DOES fire
#
#   So 255 is reserved for "this probe cannot answer the question" (its own
#   tools are missing). Every "the volume is not healthy" answer is 1: a
#   wedged disk should make the backups skip quietly and leave the loud
#   signalling to the freshness guard that watches the last-good markers,
#   rather than paging four times an hour with no new information.
#
#   The same probe is called in-script by backup-ssd.sh, pg-basebackup.sh and
#   wal-offsite.sh, so the guarantee holds however the script is invoked — not
#   only when systemd is the caller.
#
# Usage: backup-volume-check.sh [--ro|--rw] PATH
#   --ro  (default) the caller only reads PATH
#   --rw  the caller writes to PATH — also proves a write lands
#
# Exit: 0 healthy, 1 unhealthy (=> skipped), 2 usage, 255 probe unusable.
#
# Environment:
#   ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT
#                                  1 (default) => PATH must live on a mount of
#                                  its own. An unmounted /mnt/robothor-backup is
#                                  an empty directory on the root filesystem
#                                  that reads and writes perfectly, so without
#                                  this the backup lands on the root disk and
#                                  looks like success. Set to 0 only for an
#                                  instance whose backup directory is genuinely
#                                  on the root filesystem (and in tests, which
#                                  cannot create a mount unprivileged).
#   ROBOTHOR_VOLUME_PROBE_TIMEOUT  seconds allowed per step (default 20). A
#                                  dropped USB device blocks readdir forever;
#                                  without this the probe inherits the hang
#                                  and the unit sits in `activating` until
#                                  TimeoutStartSec (3600s for the nightly
#                                  backup) — worse than the failure it
#                                  replaced.
set -uo pipefail

EXIT_HEALTHY=0
EXIT_UNHEALTHY=1
EXIT_USAGE=2
EXIT_PROBE_BROKEN=255

say() { echo "backup-volume-check: $*" >&2; }

usage() {
    say "usage: backup-volume-check.sh [--ro|--rw] PATH"
    exit "$EXIT_USAGE"
}

unhealthy() {
    say "$1"
    say "-> backup volume is NOT usable; the calling unit will be skipped, not failed"
    exit "$EXIT_UNHEALTHY"
}

# ── Arguments ────────────────────────────────────────────────────────────────
MODE="--ro"
case "${1:-}" in
    --ro|--rw)
        MODE="$1"
        shift
        ;;
    --*)
        say "unknown option: $1"
        usage
        ;;
esac

TARGET="${1:-}"
[[ -n "$TARGET" ]] || usage
[[ $# -eq 1 ]] || usage

REQUIRE_SEPARATE_MOUNT="${ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT:-1}"
TIMEOUT="${ROBOTHOR_VOLUME_PROBE_TIMEOUT:-20}"
if [[ ! "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    say "ROBOTHOR_VOLUME_PROBE_TIMEOUT=${TIMEOUT} is not a positive integer"
    exit "$EXIT_USAGE"
fi

# ── The probe's own tools ────────────────────────────────────────────────────
# Missing tools mean the probe cannot tell healthy from wedged. Answering
# "unhealthy" there would silently skip every backup forever, which is the
# inert-guard failure this whole change exists to end. 255 => the unit fails
# and the operator is paged.
for tool in timeout findmnt; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        say "FATAL: $tool is not installed — the backup volume cannot be checked"
        exit "$EXIT_PROBE_BROKEN"
    fi
done

# ── 1. Does the path exist at all? ───────────────────────────────────────────
# Cheap, and it is the state a never-mounted volume leaves behind. It is NOT
# sufficient on its own — this is exactly the check that passed all through
# the outage.
if [[ ! -d "$TARGET" ]]; then
    unhealthy "$TARGET does not exist or is not a directory"
fi

# ── 2. Is the volume actually mounted? ───────────────────────────────────────
# findmnt --target resolves a path to the mount CONTAINING it. When the backup
# volume is not mounted, /mnt/robothor-backup is just an empty directory on the
# root filesystem: it stats, reads and writes fine, so every check below passes
# and the "backup" silently fills the root disk while looking like success.
# That is the `mountpoint -q` guard this probe replaces, generalised so it can
# be pointed at a path INSIDE the volume rather than at the mount point itself.
if ! MOUNT_TARGET=$(timeout "$TIMEOUT" findmnt -n -o TARGET --target "$TARGET" 2>/dev/null); then
    unhealthy "findmnt could not resolve a mount for $TARGET (timed out, or the volume is not mounted)"
fi
MOUNT_TARGET="${MOUNT_TARGET//[[:space:]]/}"

if [[ "$REQUIRE_SEPARATE_MOUNT" == "1" && "$MOUNT_TARGET" == "/" ]]; then
    unhealthy "$TARGET is on the root filesystem — the backup volume is not mounted"
fi

# ── 3. What shape is that mount in? ──────────────────────────────────────────
# `emergency_ro` is what ext4 sets when the underlying device disappears
# mid-write — the disk is gone but every stat() still succeeds.
if ! OPTIONS=$(timeout "$TIMEOUT" findmnt -n -o OPTIONS --target "$TARGET" 2>/dev/null); then
    unhealthy "findmnt could not read the mount options for $TARGET"
fi
OPTIONS="${OPTIONS//[[:space:]]/}"

if [[ "$OPTIONS" == *emergency_ro* ]]; then
    unhealthy "$TARGET is mounted emergency_ro (options: $OPTIONS) — the device dropped off the bus; reads stat() fine and writes go nowhere"
fi

if [[ "$MODE" == "--rw" ]]; then
    case ",${OPTIONS}," in
        *,rw,*) ;;
        *) unhealthy "$TARGET is not mounted read-write (options: $OPTIONS)" ;;
    esac
fi

# ── 4. A real readdir ────────────────────────────────────────────────────────
# The one syscall emergency_ro actually breaks. Nothing above this line can
# distinguish a healthy volume from a wedged one.
if ! timeout "$TIMEOUT" ls -A "$TARGET" >/dev/null 2>&1; then
    unhealthy "cannot read the contents of $TARGET (readdir failed or timed out after ${TIMEOUT}s)"
fi

# ── 5. A real write ──────────────────────────────────────────────────────────
if [[ "$MODE" == "--rw" ]]; then
    if ! PROBE_FILE=$(timeout "$TIMEOUT" mktemp "${TARGET}/.robothor-volume-probe.XXXXXX" 2>/dev/null); then
        unhealthy "cannot create a file in $TARGET — a backup written here would go nowhere"
    fi
    # "${BASH}", not `bash`: the interpreter is already running, and a caller
    # with a stripped PATH must not turn a healthy volume into a skipped unit.
    # shellcheck disable=SC2016  # $1 is the inner shell's argument, on purpose
    if ! timeout "$TIMEOUT" "${BASH:-bash}" -c 'printf ok > "$1"' bash "$PROBE_FILE" 2>/dev/null; then
        timeout "$TIMEOUT" rm -f "$PROBE_FILE" 2>/dev/null || true
        unhealthy "cannot write to a file in $TARGET — a backup written here would go nowhere"
    fi
    # A probe that litters the backup volume is a probe that gets disabled.
    if ! timeout "$TIMEOUT" rm -f "$PROBE_FILE" 2>/dev/null; then
        say "WARN: could not remove the write probe $PROBE_FILE"
    fi
fi

exit "$EXIT_HEALTHY"
