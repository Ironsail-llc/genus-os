#!/usr/bin/env bash
# The backup volume guard: detect the USB drop, heal it, page ONCE, truthfully.
#
# WHY THIS EXISTS
#   The encrypted USB backup SSD drops off the bus. Three times in nine days
#   (2026-07-14, 2026-08-24, 2026-08-27). What it leaves behind is not a clean
#   absence: the mount is still a mount, `df` still reports the cached
#   capacity, and the device-mapper node keeps a kernel reference so it cannot
#   even be closed. ext4 flips to `emergency_ro` and every write goes nowhere.
#
#   Until scripts/backup-volume-check.sh landed, all four backup units ran
#   anyway and failed — robothor-wal-offsite every 15 minutes, 96 failures a
#   day, ~22 Telegram pages whose entire content was a unit name. That probe,
#   wired as ExecCondition=, turned the storm into SILENCE: the units now SKIP.
#
#   Silence is right for a backup unit and wrong for the fleet. A skipped unit
#   fires no OnFailure=, so with the storm fixed and nothing else added, the
#   backups could stop for a week and the only evidence would be a marker file
#   nobody reads. THIS script is the thing that says so — and, because the
#   recovery is a known five-command sequence that worked by hand twice, does
#   the recovery first.
#
# WHAT IT DOES (every 10 minutes, as root)
#   healthy  and previously down -> ONE recovery notice, state cleared
#   healthy  otherwise           -> nothing at all
#   unhealthy                    -> heal (unless disabled or a backup unit is
#                                   mid-run), then page: once per heal, or
#                                   once a day while it stays down
#
#   The heal is the sequence that worked on this box, in order:
#     keyfile check                crypttab column 3 must be a readable file
#                                  before ANYTHING is torn down: a reopen we
#                                  cannot perform turns a degraded volume into
#                                  an absent one
#     cryptsetup isLuks            gate: never touch a device that is not ours
#     smartctl -d scsi -H          gate: never remount a disk SMART calls FAILED
#                                  (-d scsi, not -d sat: sat does not work
#                                  through this USB bridge)
#     findmnt -o SOURCE            gate: the thing mounted at the path must be
#     dmsetup deps / -o uuid       ours — the unmount below names a PATH, and
#                                  what makes a node ours is the DEVICE behind
#                                  it, never the name it wears
#     umount -l <mount>            the mount is wedged; a clean umount hangs
#     dmsetup info -o open         RE-READ here: umount -l returns before the
#                                  last reference is dropped, and fsck on a
#                                  referenced mapping corrupts a volume that
#                                  was only degraded
#     mount <name> <mount>         when SOME node — under any of our names — is
#                                  still the device's own live mapping and
#                                  free, this is the whole repair: no close, no
#                                  key, no fsck, and no second mapping stacked
#                                  on a disk that already has one
#     cryptsetup close <name>      a STALE node, and only when nothing holds it
#     cryptsetup open ... <name-N> a NEW mapper name, because the stale one
#                                  holds a kernel reference until reboot
#     fsck.ext4 -p                 PREEN ONLY. rc>=4 needs a human; mounting
#                                  it anyway can turn a recoverable backup
#                                  volume into an unrecoverable one
#     mount <mapper> <mount>       the SAME path — the units bind to the path,
#                                  not to the mapper name
#
# WHAT IT WILL NOT DO
#   * Heal while any backup unit is `activating`: lazy-unmounting the volume
#     out from under a running pg_basebackup corrupts the backup this guard
#     exists to protect.
#   * Answer "when did the backups last work?" from the volume that just
#     failed. Those facts come from the NVMe markers written by the backup
#     scripts (scripts/backup-state.sh) — the disk that breaks must not be the
#     disk holding the evidence.
#   * Tear down what it cannot rebuild: no keyfile in crypttab, or a mapping
#     something still holds, means it pages and touches nothing.
#   * Unmount somebody else's filesystem. `umount -l` names a PATH and this
#     runs as root, so what is mounted at the mountpoint is identified first
#     and anything that is not this guard's own mapper is refused.
#   * Mask a flaky bridge. EVERY heal pages, under its own dedup key, because
#     three self-healed drops in a day is a hardware fault, not a quiet night.
#
# STATE  ${ROBOTHOR_VOLUME_GUARD_STATE_DIR:-/run/robothor/volume-guard}
#   down_since  when the volume first failed a probe (edge-triggered, so the
#               page can say how long it has been down)
#   last_paged  epoch of the last DOWN page, for the repage interval
#   heal_count  USB drops healed since boot — the "#N" in the heal page
#   On /run (tmpfs) on purpose, like robothor/engine/manifest_guard.py: a
#   reboot re-arms the guard, and "since boot" is exactly the window in which
#   a repeated drop means the hardware is failing.
#
# ENVIRONMENT
#   ROBOTHOR_BACKUP_MOUNT                  mount point (default
#                                          /mnt/robothor-backup)
#   ROBOTHOR_VOLUME_GUARD_STATE_DIR        see STATE above
#   ROBOTHOR_BACKUP_STATE_DIR              last-good markers (NVMe)
#   ROBOTHOR_CRYPTTAB                      crypttab to resolve the UUID from
#                                          (default /etc/crypttab)
#   ROBOTHOR_VOLUME_GUARD_MAPPER           crypttab name of the container
#                                          (default robothor-backup)
#   ROBOTHOR_VOLUME_GUARD_HEAL             1 (default) to heal, 0 to page only
#   ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS   quiet period while still down
#                                          (default 86400)
#   ROBOTHOR_VOLUME_GUARD_CHECK_CMD        the volume probe (test seam)
#   ROBOTHOR_VOLUME_GUARD_ALERT_CMD        the pager (test seam)
#   ROBOTHOR_VOLUME_GUARD_DEV_DIR          by-uuid dir (test seam)
#   ROBOTHOR_VOLUME_GUARD_MAPPER_DIR       /dev/mapper (test seam)
#
# Exit: 0 whatever the volume's state — a down volume is news, not a unit
#       failure. 1 ONLY when a page could not be DELIVERED, so this unit's own
#       OnFailure=robothor-alert@%n.service fires (same discipline as
#       scripts/liveness_probe.sh: an undelivered page is not success).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# Last-good markers. Sourced, not reimplemented: the format and the "unknown
# (no successful run recorded)" fallback have to match what the backup scripts
# write, and an empty string where a timestamp belongs reads as "just now".
#
# Checked, because `source` on a missing file is not fatal here — there is no
# `set -e`, and there must not be one: this script has to keep going through
# failing sub-commands to reach the page. Without the library every LAST_*
# would be empty and the page would read "Local dump last good:" with nothing
# after it, which is scanned as "fine". Exit 1 instead, so the guard's own
# OnFailure= pages about a guard that cannot tell the truth.
if [[ ! -r "${SCRIPT_DIR}/backup-state.sh" ]]; then
    echo "backup-volume-guard: ${SCRIPT_DIR}/backup-state.sh missing — cannot report last-good facts" >&2
    exit 1
fi
# shellcheck source=./backup-state.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/backup-state.sh"

MOUNT="${ROBOTHOR_BACKUP_MOUNT:-/mnt/robothor-backup}"
STATE_DIR="${ROBOTHOR_VOLUME_GUARD_STATE_DIR:-/run/robothor/volume-guard}"
CRYPTTAB="${ROBOTHOR_CRYPTTAB:-/etc/crypttab}"
MAPPER_BASE="${ROBOTHOR_VOLUME_GUARD_MAPPER:-robothor-backup}"
DEV_DIR="${ROBOTHOR_VOLUME_GUARD_DEV_DIR:-/dev/disk/by-uuid}"
MAPPER_DIR="${ROBOTHOR_VOLUME_GUARD_MAPPER_DIR:-/dev/mapper}"
CHECK_CMD="${ROBOTHOR_VOLUME_GUARD_CHECK_CMD:-/usr/bin/env bash ${SCRIPT_DIR}/backup-volume-check.sh}"
ALERT_CMD="${ROBOTHOR_VOLUME_GUARD_ALERT_CMD:-/usr/bin/env bash ${SCRIPT_DIR}/send_failure_alert.sh}"
HEAL="${ROBOTHOR_VOLUME_GUARD_HEAL:-1}"
REPAGE_SECONDS="${ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS:-86400}"

# Units that write to the volume. Lazy-unmounting under any of them corrupts
# the backup, so any one of them `activating` defers the heal to the next tick.
# robothor-wal-offsite is here because it WRITES to the volume (it copies base
# backups off it and reads the prune horizon from it), not because it stops
# when the volume is down: it degrades instead, and keeps archiving new WAL
# segments to the remote. That distinction is what the DOWN page has to say.
BACKUP_UNITS=(
    robothor-backup-local.service
    robothor-backup-offsite.service
    robothor-backup-verify.service
    robothor-basebackup.service
    robothor-wal-offsite.service
)

log() { echo "backup-volume-guard: $*"; }
err() { echo "backup-volume-guard: $*" >&2; }

if [[ ! "$REPAGE_SECONDS" =~ ^[0-9]+$ ]]; then
    err "ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS=${REPAGE_SECONDS} is not an integer — using 86400"
    REPAGE_SECONDS=86400
fi

# ── State ────────────────────────────────────────────────────────────────────
if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
    err "cannot create the state directory ${STATE_DIR} — the guard cannot tell a"
    err "new outage from an old one, and would page every 10 minutes"
    exit 1
fi

state_read() {
    local file="${STATE_DIR}/$1"
    [[ -r "$file" ]] && head -n 1 "$file" 2>/dev/null | tr -d '[:space:]'
    return 0
}
state_write() { printf '%s\n' "$2" >"${STATE_DIR}/$1" 2>/dev/null || err "cannot write ${STATE_DIR}/$1"; }
state_clear() { rm -f "${STATE_DIR}/$1" 2>/dev/null || true; }

# The lock lives BESIDE the state dir, not in it: a lock file inside would be
# indistinguishable from state to anything (a human included) reading the dir.
#
# Serialised because a heal takes minutes — an fsck on a full 4TB volume can
# outlast the 10-minute timer — and two concurrent runs would unmount the
# volume the other one just mounted.
LOCK_FILE="${STATE_DIR%/}.lock"
lock_held=1
exec 9>"$LOCK_FILE" 2>/dev/null || lock_held=0
if ((lock_held == 0)); then
    # Say so instead of skipping. `flock -n 9` on a closed descriptor fails
    # exactly like a held lock, and treating that as "another run has it" would
    # make the guard exit 0 forever without ever probing the volume — the inert
    # control, arrived at by a redirect nobody checked.
    err "cannot open the lock file ${LOCK_FILE} — running WITHOUT serialisation"
elif command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        log "another guard run is still working (probably a long fsck) — skipping this tick"
        exit 0
    fi
fi

# ── The probe ────────────────────────────────────────────────────────────────
# Not reimplemented here: scripts/backup-volume-check.sh is the SAME probe the
# backup units use as ExecCondition=, so the guard cannot disagree with the
# thing that decides whether they run.
PROBE_OUTPUT=""
probe() {
    local argv rc=0
    # Split on whitespace deliberately: the seam is a command LINE, not a path
    # ("/usr/bin/env bash /…/backup-volume-check.sh"). The consequence is that
    # neither the checkout nor ROBOTHOR_WORKSPACE may contain a space — the
    # default is built from SCRIPT_DIR. The same applies to ALERT_CMD below.
    read -r -a argv <<<"$CHECK_CMD"
    PROBE_OUTPUT="$("${argv[@]}" --rw "$MOUNT" 2>&1 >/dev/null)" || rc=$?
    return "$rc"
}

# ── The pager ────────────────────────────────────────────────────────────────
# Returns the SENDER's status. Checked, never assumed: an undelivered page is
# not a page (robothor/engine/alerts.py, scripts/liveness_probe.sh).
page() {
    local key="$1" body="$2" argv
    # A command line, split on whitespace — see probe(): no spaces in the path.
    read -r -a argv <<<"$ALERT_CMD"
    if "${argv[@]}" "$key" "$body"; then
        return 0
    fi
    err "the page was NOT delivered — the sender failed. Failing this unit so its"
    err "own OnFailure= hook fires; the outage must not end up silent."
    return 1
}

# ── Facts about the backups, read from NVMe ──────────────────────────────────
LAST_LOCAL_DUMP="$(backup_state_last last-local-dump)"
LAST_OFFSITE="$(backup_state_last last-offsite-ok)"
LAST_WAL_OFFSITE="$(backup_state_last last-wal-offsite-ok)"

# ── The device behind the mount ──────────────────────────────────────────────
# From crypttab, not from the mount: when the volume is wedged the mount tells
# you nothing about which physical device it was supposed to be.
DEVICE=""
KEYFILE=""
DEVICE_REASON=""
# The LUKS container's own UUID, dashes stripped and lowercased, as it appears
# inside a dm-crypt node's UUID (CRYPT-LUKS2-<uuid>-<name>). For a LUKS
# partition the by-uuid name IS the header UUID, so crypttab already carries
# it. Empty when crypttab names a path instead — see node_is_our_luks_corpse.
LUKS_UUID=""
resolve_device() {
    if [[ ! -r "$CRYPTTAB" ]]; then
        DEVICE_REASON="cannot read ${CRYPTTAB} — the backing device is unknown"
        return 1
    fi
    local spec
    spec="$(awk -v name="$MAPPER_BASE" \
        '!/^[[:space:]]*#/ && $1 == name { print $2; exit }' "$CRYPTTAB")"
    KEYFILE="$(awk -v name="$MAPPER_BASE" \
        '!/^[[:space:]]*#/ && $1 == name { print $3; exit }' "$CRYPTTAB")"
    if [[ -z "$spec" ]]; then
        DEVICE_REASON="no crypttab entry named ${MAPPER_BASE} in ${CRYPTTAB}"
        return 1
    fi
    case "$spec" in
        UUID=*)
            DEVICE="${DEV_DIR}/${spec#UUID=}"
            LUKS_UUID="${spec#UUID=}"
            LUKS_UUID="${LUKS_UUID//-/}"
            LUKS_UUID="${LUKS_UUID,,}"
            ;;
        /*) DEVICE="$spec" ;;
        *)
            DEVICE_REASON="crypttab entry ${MAPPER_BASE} names ${spec}, which is neither a UUID= nor a path"
            return 1
            ;;
    esac
    return 0
}

# major:minor as MAJ:MIN, whichever punctuation the tool used.
majmin() { sed -n 's/.*(\([0-9]*\)[,:][[:space:]]*\([0-9]*\)).*/\1:\2/p' | head -n 1; }

# Does anything still hold this mapping? Asked of the kernel, every time, at
# the moment it matters — never inherited from an earlier step.
#
# `umount -l` detaches the tree and RETURNS; the last reference is dropped
# whenever the last user lets go, which may be after this script has moved on.
# So a count read before the unmount says nothing about the mapping fsck is
# about to be pointed at, and fsck on a mapping the kernel is still handing out
# turns a degraded volume into a corrupted one.
MAPPER_OPEN_COUNT=""
mapper_is_free() {
    # A dmsetup that fails, or answers with nothing, leaves this empty and the
    # test below says BUSY — deliberately: the only thing downstream of this
    # question is an fsck, and "I could not ask" is not an answer of zero.
    MAPPER_OPEN_COUNT="$(dmsetup info -c --noheadings -o open "$1" 2>/dev/null | tr -dc '0-9')"
    [[ "$MAPPER_OPEN_COUNT" == "0" ]]
}

# ── Heal ─────────────────────────────────────────────────────────────────────
HEAL_REASON=""
MAPPER_USED=""

# ── Identity: by the DEVICE, never by the name ───────────────────────────────
#
# `robothor-backup-<token>` is a NAME. Anything with root can create a mapper
# node under one, and a check that accepts a name lazy-unmounts whatever wears
# it. What makes a node this guard's is the DEVICE behind it, established two
# ways because a drop takes one of them away:
#
#   deps  — `dmsetup deps` gives the major:minor the node is backed by, and the
#           crypttab UUID -> by-uuid -> lsblk chain gives the device's. Equal
#           means this is the device's own LIVE mapping.
#   uuid  — a dm-crypt node's UUID is CRYPT-LUKS<n>-<container uuid>-<name>.
#           That names the LUKS CONTAINER, not the bus address, so it still
#           identifies our own corpse after the device has dropped off and the
#           deps have gone (an `error` target has none) or gone stale. This is
#           the 2026-08-27 signature: the wedged node mounted at the path is
#           ours and unmounting it is the entire recovery, so identity that
#           only knew `deps` would refuse to heal the case the guard is for.
MAPPER_DEPS=""
DEVICE_MAJMIN=""

# Resolved once per tick: the device does not move under us mid-heal.
device_majmin() {
    [[ -n "$DEVICE_MAJMIN" ]] && return 0
    DEVICE_MAJMIN="$(lsblk -no MAJ:MIN "$DEVICE" 2>/dev/null | head -n 1 | tr -d '[:space:]')"
    [[ -n "$DEVICE_MAJMIN" ]]
}

node_is_backed_by_device() {
    MAPPER_DEPS="$(dmsetup deps -o devno "$1" 2>/dev/null | majmin)"
    device_majmin || return 1
    [[ -n "$MAPPER_DEPS" && "$MAPPER_DEPS" == "$DEVICE_MAJMIN" ]]
}

node_is_our_luks_corpse() {
    [[ -n "$LUKS_UUID" ]] || return 1
    local dm_uuid
    dm_uuid="$(dmsetup info -c --noheadings -o uuid "$1" 2>/dev/null | tr -d '[:space:]')"
    [[ "$dm_uuid" == CRYPT-LUKS* ]] || return 1
    dm_uuid="${dm_uuid//-/}"
    [[ "${dm_uuid,,}" == *"$LUKS_UUID"* ]]
}

# Ours = the device's live mapping, or our own container's corpse.
node_is_ours() { node_is_backed_by_device "$1" || node_is_our_luks_corpse "$1"; }

# The node name a mount SOURCE names, or nothing when the source is not one of
# this guard's mapper nodes at all. The cheap check, first: it must live in
# ${MAPPER_DIR} with no further slash (so <name>-1/../../sda1 is not ours), and
# be ${MAPPER_BASE} plus at most ONE trailing -<token> of letters, digits and
# underscores — a heal's <name>-N, and also the name a human recovered under by
# hand. A name ours merely PREFIXES (robothor-backupX, robothor-backup-1-other)
# is a different mapping.
mapper_node_name() {
    local src="$1" name
    [[ "$src" == "${MAPPER_DIR}/"* ]] || return 0
    name="${src#"${MAPPER_DIR}/"}"
    [[ "$name" == */* ]] && return 0
    [[ "$name" == "$MAPPER_BASE" || "$name" =~ ^"${MAPPER_BASE}"-[[:alnum:]_]+$ ]] || return 0
    printf '%s' "$name"
}

# Which node, if any, IS the device's own mapping — whatever it is called.
#
# Asked of every node wearing this guard's name, not just the bare one. A drop
# is healed under a NEW name (the stale node holds a kernel reference until
# reboot), so the live mapping is `<name>-1` after one heal and `<name>-b` on
# this box right now. Looking only at ${MAPPER_BASE} meant calling the corpse
# there "stale", opening a SECOND LUKS mapping over a disk that already had a
# live one, and abandoning the node that was already correct — one more burned
# name per tick, and nine of those are a reboot.
#
# A free node wins; a node that is backed but busy is still returned, because
# "something holds the live mapping" must stop the heal, never license a second
# mapping over the same disk.
LIVE_NODE=""
select_live_node() {
    LIVE_NODE=""
    local candidate node first_backed=""

    # The one at the mountpoint first: its identity is already established, and
    # its open count cannot be asked yet — the mount itself is an opener.
    if [[ -n "$MOUNTED_NAME" ]] && node_is_backed_by_device "$MOUNTED_NAME"; then
        LIVE_NODE="$MOUNTED_NAME"
        log "${LIVE_NODE} is still backed by ${MAPPER_DEPS}, which is the device — reusing it"
        return 0
    fi

    for candidate in "${MAPPER_DIR}/${MAPPER_BASE}" "${MAPPER_DIR}/${MAPPER_BASE}"-*; do
        [[ -e "$candidate" ]] || continue
        node="$(mapper_node_name "$candidate")"
        [[ -n "$node" && "$node" != "$MOUNTED_NAME" ]] || continue
        node_is_backed_by_device "$node" || continue
        [[ -n "$first_backed" ]] || first_backed="$node"
        mapper_is_free "$node" || continue
        LIVE_NODE="$node"
        log "${LIVE_NODE} is backed by ${MAPPER_DEPS}, which is the device — reusing it"
        return 0
    done

    if [[ -n "$first_backed" ]]; then
        LIVE_NODE="$first_backed"
        log "${LIVE_NODE} is backed by the device but held — reusing it, or refusing"
        return 0
    fi
    log "no mapper node is backed by the device (${DEVICE_MAJMIN:-unknown}) — a reopen is unavoidable"
    return 1
}

# crypttab's third column is the ONLY key a timer can use: `none`, `-`, or a
# file it cannot read all mean the same thing here — the container can be
# closed but never reopened without a human at a console.
keyfile_is_usable() {
    [[ -n "$KEYFILE" && "$KEYFILE" != "none" && "$KEYFILE" != "-" ]] || return 1
    [[ -f "$KEYFILE" && -r "$KEYFILE" ]]
}

# A free name to open the container under. Reached only when NOTHING is backed
# by the device — select_live_node has already refused to stack a second
# mapping on a disk that has a live one.
pick_mapper_name() {
    if [[ ! -e "${MAPPER_DIR}/${MAPPER_BASE}" ]]; then
        MAPPER_USED="$MAPPER_BASE"
        return 0
    fi

    local i
    for i in 1 2 3 4 5 6 7 8 9; do
        if [[ ! -e "${MAPPER_DIR}/${MAPPER_BASE}-${i}" ]]; then
            MAPPER_USED="${MAPPER_BASE}-${i}"
            return 0
        fi
    done
    # Every name burned means nine kernel references that never went away.
    # Nothing this script can do clears them.
    HEAL_REASON="9 stale mappings, reboot required"
    return 1
}

# SMART, through the bridge that actually answers. `-d sat` returns nothing on
# this USB enclosure; `-d scsi` works. A missing smartctl is not a reason to
# refuse the heal — it is a reason to say the gate did not run.
smart_gate() {
    if ! command -v smartctl >/dev/null 2>&1; then
        log "smartctl is not installed — the SMART gate did not run"
        return 0
    fi
    local disk out
    disk="$(lsblk -no PKNAME "$DEVICE" 2>/dev/null | head -n 1 | tr -d '[:space:]')"
    if [[ -n "$disk" ]]; then disk="/dev/${disk}"; else disk="$DEVICE"; fi
    out="$(smartctl -d scsi -H "$disk" 2>&1)"
    # Case-sensitive: the health verdict is the literal token FAILED. A
    # case-insensitive match would trip on "Read Device Identity failed",
    # which is a bridge quirk, not a dying disk.
    if grep -q 'FAILED' <<<"$out"; then
        HEAL_REASON="SMART reports ${disk} as FAILED — refusing to remount a dying disk"
        return 1
    fi
    return 0
}

# What is mounted at ${MOUNT}, expressed as one of this guard's node names —
# empty when nothing is mounted there. Read-only, and it runs FIRST, because
# every side effect below is aimed at whatever it decides.
#
# `umount -l` names a PATH, and this runs as root: whatever is mounted there is
# what gets detached. The mountpoint is an ordinary directory anything can be
# mounted over — a rescue image, a staging tree, another disk parked there
# while the real one was away — and in every one of those cases the probe fails
# for the RIGHT reason and the unmount would be aimed at the wrong filesystem.
#
# The name is the CHEAP check and it is not sufficient: any node can be created
# under `robothor-backup-<token>`, and a guard that trusted the spelling would
# lazily unmount it. So the name only says which node to ask about, and the
# DEVICE decides — node_is_ours above.
MOUNTED_NAME=""
identify_mounted_source() {
    MOUNTED_NAME=""
    findmnt -rn -o TARGET --mountpoint "$MOUNT" >/dev/null 2>&1 || return 0
    local mounted_source name
    mounted_source="$(findmnt -rn -o SOURCE --mountpoint "$MOUNT" 2>/dev/null | head -n 1)"
    name="$(mapper_node_name "$mounted_source")"
    if [[ -z "$name" ]]; then
        HEAL_REASON="something other than the backup mapper is mounted at ${MOUNT} (${mounted_source:-unknown}) — refusing to unmount it"
        return 1
    fi
    if ! node_is_ours "$name"; then
        HEAL_REASON="something other than the backup mapper is mounted at ${MOUNT} (${mounted_source} is not backed by ${DEVICE}) — refusing to unmount it"
        return 1
    fi
    MOUNTED_NAME="$name"
    return 0
}

heal() {
    HEAL_REASON=""
    MAPPER_USED=""
    local opened_here=0 reuse=0

    # 1. Identity, before anything is touched and by the DEVICE rather than by
    #    a name: what is at the mountpoint, and which node — under any name —
    #    IS the mapping of the device that came back.
    identify_mounted_source || return 1
    select_live_node && reuse=1

    # 2. Before the first side effect: can this heal put the volume BACK?
    #
    #    The teardown (umount, close) is easy and the rebuild is the part that
    #    needs a key. Doing them in that order without checking meant a
    #    crypttab with no keyfile turned a DEGRADED volume — wedged, but with
    #    its mapping intact — into an ABSENT one that only a human at a console
    #    can restore. A reopen is needed only when nothing is already the
    #    device's own mapping; anything we reuse, we merely mount.
    if ((reuse == 0)) && ! keyfile_is_usable; then
        HEAL_REASON="no non-interactive keyfile in crypttab (column 3 = ${KEYFILE:-<empty>}); refusing to tear down a mapping I cannot rebuild — fix crypttab or reboot"
        return 1
    fi

    # 3. Gates, and they come before every side effect because they are
    #    read-only: nothing below may run against a device that is not ours, or
    #    a disk the firmware has given up on — including the unmount.
    if ! cryptsetup isLuks "$DEVICE" >/dev/null 2>&1; then
        HEAL_REASON="${DEVICE} is not a LUKS container — refusing to touch it"
        return 1
    fi
    smart_gate || return 1

    # 4. Let go of the wedged mount. Lazy, because a plain umount blocks
    #    forever on a device that is gone. Identified in step 1.
    if [[ -n "$MOUNTED_NAME" ]]; then
        if ! umount -l "$MOUNT"; then
            HEAL_REASON="umount -l ${MOUNT} failed"
            return 1
        fi
    fi

    if ((reuse)); then
        # 5a. The mapping is the device's own. `umount -l` returns before the
        #     last reference is dropped, so ask the kernel now — everything
        #     below this point would be done TO this mapping.
        if ! mapper_is_free "$LIVE_NODE"; then
            HEAL_REASON="mapper ${LIVE_NODE} still has ${MAPPER_OPEN_COUNT:-unknown} opener(s) after umount -l — refusing to fsck a referenced mapping; the volume is now UNMOUNTED and the next tick remounts it once the holder lets go"
            return 1
        fi
        # 5b. The cheapest repair that can work, tried first: put the existing
        #     mapping back at its path. No close, no key, no fsck — and
        #     nothing to rebuild if it works. Most wedges are exactly this.
        if mount "${MAPPER_DIR}/${LIVE_NODE}" "$MOUNT"; then
            MAPPER_USED="$LIVE_NODE"
            return 0
        fi
        log "remounting the live ${LIVE_NODE} failed — repairing it instead"
        # Repaired under its OWN name: the mapping is already the device's, so
        # there is nothing to reopen and no second mapping to stack on it.
        MAPPER_USED="$LIVE_NODE"
    else
        # 5c. Nothing is backed by the device: every node wearing this guard's
        #     name is a corpse. Close the one we just unmounted (or the bare
        #     node, when nothing was mounted) IF nothing holds it; usually
        #     something does, and that kernel reference is why a new name is
        #     needed.
        local stale_node="$MOUNTED_NAME"
        [[ -n "$stale_node" ]] || stale_node="$MAPPER_BASE"
        # ${MAPPER_BASE} is where that fallback lands, and it is a NAME: the
        # one node in this script reached without going through identity
        # first. A close DESTROYS a mapping, so a stranger parked under our
        # bare name must be stepped over, not torn down — pick_mapper_name
        # then takes the first free alternate, exactly as it does for a node
        # of ours the kernel will not let go of.
        if [[ -e "${MAPPER_DIR}/${stale_node}" ]] && ! node_is_ours "$stale_node"; then
            log "${stale_node} is not ours (neither backed by ${DEVICE} nor our container) — leaving it alone"
            stale_node=""
        fi
        if [[ -n "$stale_node" && -e "${MAPPER_DIR}/${stale_node}" ]]; then
            if mapper_is_free "$stale_node"; then
                cryptsetup close "$stale_node" >/dev/null 2>&1 \
                    || log "cryptsetup close ${stale_node} failed — carrying on under a new name"
            else
                log "${stale_node} has open count ${MAPPER_OPEN_COUNT:-unknown} — cannot be closed until reboot"
            fi
        fi

        # 5d. Open under a free name.
        pick_mapper_name || return 1
        if [[ ! -e "${MAPPER_DIR}/${MAPPER_USED}" ]]; then
            local open_argv=(cryptsetup open "$DEVICE" "$MAPPER_USED")
            if keyfile_is_usable; then
                open_argv+=(--key-file "$KEYFILE")
            fi
            if ! "${open_argv[@]}"; then
                HEAL_REASON="cryptsetup open failed for ${DEVICE} as ${MAPPER_USED}"
                return 1
            fi
            opened_here=1
        fi
    fi

    # 6. The mapping fsck is about to touch must be unreferenced, and that is
    #    established HERE, against the name actually chosen — not inferred from
    #    the count read before `umount -l`, which returns early by design.
    #
    #    The question is whether the guard opened this mapping ITSELF, not what
    #    it is called. A container opened above is ours alone and nothing has
    #    had the chance to reference it — asking anyway refused heals whose
    #    every step had just succeeded. Anything else is a mapping that was
    #    somebody's before this run, whatever name it wears: the original, or a
    #    <name>-N that appeared between pick_mapper_name and the open. When it
    #    is busy there is nothing safe left to do to it, so the guard stops and
    #    pages rather than repairing a filesystem out from under its users.
    if ((opened_here == 0)) && ! mapper_is_free "$MAPPER_USED"; then
        HEAL_REASON="mapper ${MAPPER_USED} still has ${MAPPER_OPEN_COUNT:-unknown} opener(s) after umount -l — refusing to fsck a referenced mapping; the volume is now UNMOUNTED and the next tick remounts it once the holder lets go"
        return 1
    fi

    # 7. Preen fsck ONLY. -p fixes what is safe to fix without asking and
    #    exits >=4 for anything that needs a decision. An automated -y here
    #    could destroy a recoverable filesystem while nobody is watching.
    local fsck_rc=0
    fsck.ext4 -p "${MAPPER_DIR}/${MAPPER_USED}" || fsck_rc=$?
    if ((fsck_rc >= 4)); then
        ((opened_here)) && { cryptsetup close "$MAPPER_USED" >/dev/null 2>&1 \
            || log "could not close ${MAPPER_USED} after the failed fsck"; }
        HEAL_REASON="filesystem needs manual fsck (fsck.ext4 -p exit ${fsck_rc}) — NOT mounted"
        return 1
    fi

    # 8. Back at the SAME path: RequiresMountsFor= and every backup script
    #    name the path, not the mapper.
    #
    #    A mapping this run opened and could not mount is closed again. Left
    #    behind it is one more node holding one more name, and nine of those
    #    are a reboot — the guard must not manufacture the condition it exists
    #    to recover from. A mapping we merely reused is left exactly as found.
    if ! mount "${MAPPER_DIR}/${MAPPER_USED}" "$MOUNT"; then
        ((opened_here)) && { cryptsetup close "$MAPPER_USED" >/dev/null 2>&1 \
            || log "could not close ${MAPPER_USED} after the failed mount"; }
        HEAL_REASON="mount ${MAPPER_DIR}/${MAPPER_USED} at ${MOUNT} failed"
        return 1
    fi
    return 0
}

busy_backup_unit() {
    command -v systemctl >/dev/null 2>&1 || return 1
    local unit state
    for unit in "${BACKUP_UNITS[@]}"; do
        state="$(systemctl is-active "$unit" 2>/dev/null | head -n 1 | tr -d '[:space:]')"
        if [[ "$state" == "activating" ]]; then
            printf '%s' "$unit"
            return 0
        fi
    done
    return 1
}

# ── Healthy ──────────────────────────────────────────────────────────────────
probe_rc=0
probe || probe_rc=$?

if ((probe_rc == 0)); then
    down_since="$(state_read down_since)"
    if [[ -n "$down_since" ]]; then
        log "the backup volume is healthy again (down since ${down_since})"
        if ! page "backup-volume-recovered" \
            "✅ backup volume healthy again (down since ${down_since}; the guard did not heal it — the device came back, or it was fixed by hand).
Local dump last good: ${LAST_LOCAL_DUMP}
Offsite last OK: ${LAST_OFFSITE}
WAL offsite last OK: ${LAST_WAL_OFFSITE}"; then
            exit 1
        fi
    fi
    # Cleared even when nothing was down: a stale last_paged would suppress the
    # first page of the NEXT outage for a whole day. heal_count is deliberately
    # kept — it counts drops since boot, and tmpfs is what resets it.
    state_clear down_since
    state_clear last_paged
    log "volume healthy at ${MOUNT}"
    exit 0
fi

# ── Unhealthy ────────────────────────────────────────────────────────────────
# The probe's own words, so the page says what is actually wrong (emergency_ro,
# not mounted, readdir timed out) rather than "exit 1".
reason="$(grep -m 1 '^backup-volume-check: ' <<<"$PROBE_OUTPUT" \
    | sed 's/^backup-volume-check: //')"
[[ -n "$reason" ]] || reason="the volume probe reported it unusable (exit ${probe_rc})"
((probe_rc == 255)) && reason="the volume probe itself is broken (exit 255): ${reason}"

DOWN_SINCE="$(state_read down_since)"
if [[ -z "$DOWN_SINCE" ]]; then
    DOWN_SINCE="$(date -Is)"
    state_write down_since "$DOWN_SINCE"
fi
err "backup volume at ${MOUNT} is NOT usable: ${reason}"

healed=0
if ! resolve_device; then
    reason="$DEVICE_REASON"
    log "not healing: ${reason}"
elif [[ ! -e "$DEVICE" ]]; then
    # Nothing to unmount, open or fsck — the disk is not on the bus. Say so
    # and stop: the fix is physical.
    reason="device absent from USB"
    log "not healing: ${reason} (${DEVICE} does not exist)"
elif [[ "$HEAL" != "1" ]]; then
    log "not healing: ROBOTHOR_VOLUME_GUARD_HEAL=${HEAL}"
elif busy_unit="$(busy_backup_unit)"; then
    reason="${reason}; heal deferred: ${busy_unit} is activating"
    log "not healing: ${busy_unit} is activating — unmounting under a running backup would corrupt it"
else
    if heal; then
        # PROVE it. The heal steps returning 0 is not the same as a usable
        # volume, and "recovered" in a log nobody can falsify is how an inert
        # control survives for months.
        reprobe_rc=0
        probe || reprobe_rc=$?
        if ((reprobe_rc == 0)); then
            healed=1
        else
            reason="remapped as ${MAPPER_USED}, but the volume is still unusable"
        fi
    else
        reason="$HEAL_REASON"
    fi
fi

if ((healed)); then
    count="$(state_read heal_count)"
    [[ "$count" =~ ^[0-9]+$ ]] || count=0
    count=$((count + 1))
    state_write heal_count "$count"
    log "backup volume auto-recovered (drop #${count} since boot; remapped as ${MAPPER_USED})"
    # A unique key per drop, on purpose: the sender dedups per key for an hour,
    # and a bridge that flaps twice in that hour must produce two pages.
    if ! page "backup-volume-auto-recovered-${count}" \
        "⚠️ backup volume auto-recovered (USB drop #${count} since boot; remapped as ${MAPPER_USED}).
Local dump last good: ${LAST_LOCAL_DUMP}
Offsite last OK: ${LAST_OFFSITE}
WAL offsite last OK: ${LAST_WAL_OFFSITE}"; then
        exit 1
    fi
    state_clear down_since
    state_clear last_paged
    exit 0
fi

# ── Still down: one page, then quiet ─────────────────────────────────────────
last_paged="$(state_read last_paged)"
now="$(date +%s)"
if [[ "$last_paged" =~ ^[0-9]+$ ]] && ((now - last_paged < REPAGE_SECONDS)); then
    log "already paged $((now - last_paged))s ago (repage after ${REPAGE_SECONDS}s) — staying quiet"
    exit 0
fi

if ! page "backup-volume-down" \
    "🔴 BACKUP VOLUME DOWN since ${DOWN_SINCE} (${reason}).
Paused: nightly dump (last good ${LAST_LOCAL_DUMP}), offsite refresh (last OK ${LAST_OFFSITE}), base backup + WAL prune.
Still running: WAL offsite replication of NEW segments (last OK ${LAST_WAL_OFFSITE}) — base-backup copy and WAL prune paused → PITR RPO intact, dump-tier RPO growing.
Runbook: BACKUP_VOLUME_GUARD.md"; then
    # Deliberately NOT stamping last_paged: arming the day-long quiet period on
    # a page nobody received is how an outage goes silent.
    exit 1
fi
state_write last_paged "$now"
exit 0
