# Backup Volume Guard

The runbook the guard's pages point at. It answers one question: **the backup
volume paged — now what?**

## What the guard is

The encrypted USB backup SSD drops off the bus. Three times in nine days
(2026-07-14, 2026-08-24, 2026-08-27). What that leaves behind is not a clean
absence:

- the mount is still a mount and `df` still reports the cached capacity;
- ext4 has flipped to `emergency_ro`, so `stat()` works and every write goes
  nowhere;
- the device-mapper node keeps a kernel reference, so it cannot be closed even
  after the device comes back.

`scripts/backup-volume-check.sh`, wired as `ExecCondition=` on the four backup
units, stopped the resulting page storm (96 `robothor-wal-offsite` failures a
day, ~22 pages whose entire content was a unit name) by making those units
**skip**. A skipped unit fires no `OnFailure=`, so on its own that fix trades a
storm for silence.

`robothor-backup-volume-guard.timer` is the other half. Every 10 minutes (and
3 minutes after boot) it runs `scripts/backup-volume-guard.sh` as root, which:

1. asks the same probe whether the volume is usable;
2. if it is not, performs the recovery that worked by hand twice on this box;
3. pages **once per heal**, or **once a day** while the volume stays down.

## The three pages

| Page | Means | Action |
|---|---|---|
| `🔴 BACKUP VOLUME DOWN since T (reason)` | The volume is unusable and the guard could not fix it. | Read `reason`, then the sections below. Repeats once every `ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS` (default 24h) until fixed. |
| `⚠️ backup volume auto-recovered (USB drop #N since boot; remapped as X)` | The guard fixed it. Backups are running again. | Nothing urgent. **But `#N` is the count since boot: two or more in a day is a failing bridge/cable, not a fluke.** |
| `✅ backup volume healthy again` | It recovered without the guard (replugged, or fixed by hand). | Nothing. The guard has re-armed. |

Each page carries the last-good marker for every backup tier, read from
`/var/lib/robothor/backup-state` on **NVMe** — never from the volume that just
failed. `unknown (no successful run recorded)` means exactly that; it does not
mean recent.

## Reading the reason

| Reason in the page | What happened | Fix |
|---|---|---|
| `device absent from USB` | The disk is not on the bus at all. The guard took no action, on purpose. | Physical: reseat the cable, try another port, check `dmesg -T \| tail -40` for the disconnect. Once it enumerates, the next tick heals it. |
| `9 stale mappings, reboot required` | Nine device-mapper nodes are held by kernel references that nothing in userspace can release. | Reboot. This is the only thing that clears them. |
| `filesystem needs manual fsck (fsck.ext4 -p exit N) — NOT mounted` | Preen could not repair it without a decision. The guard deliberately did **not** mount it and closed the container again. | `cryptsetup open /dev/disk/by-uuid/<uuid> robothor-backup-<n>` then `fsck.ext4 -y /dev/mapper/robothor-backup-<n>`, *with the offsite copy verified first*. |
| `SMART reports /dev/sdX as FAILED` | The firmware has given up on the disk. The guard refuses to remount it. | Replace the disk. Restore from offsite (`docs/runbooks/OFFSITE_BACKUP.md`). |
| `mapper robothor-backup still has N opener(s) after umount -l — refusing to fsck a referenced mapping` | The mapping is the device's own and correct, but something still holds it: `umount -l` returns before the last reference is dropped. `fsck.ext4 -p` there would corrupt a filesystem that was only degraded. | `lsof +f -- /dev/mapper/robothor-backup` / `fuser -vm /mnt/robothor-backup` to find the holder, stop it, and the next tick heals. If nothing is holding it, the reference is a kernel one: reboot. |
| `no non-interactive keyfile in crypttab (column 3 = X); refusing to tear down a mapping I cannot rebuild` | crypttab column 3 is `none`, `-`, or a file root cannot read, so `cryptsetup open` would prompt on a console the timer does not have. The guard did **not** unmount or close anything — it left the volume degraded rather than making it absent. | Put a readable keyfile in column 3 of `/etc/crypttab` (`cryptsetup luksAddKey` first), or run the manual procedure below and unlock it by hand. |
| `... heal deferred: <unit> is activating` | A backup unit was mid-run; unmounting under it would corrupt the backup. | Nothing — the next tick heals it. |
| `<mount> is mounted emergency_ro ...` with `HEAL=0` | Healing is switched off. | Re-enable, or run the manual procedure below. |

## The manual procedure

Exactly what the guard automates. Run it as root when the guard is disabled or
when you want to watch it happen.

```bash
# 0. Can you get back in? crypttab column 3 must be a keyfile you can read;
#    `none` or `-` means the guard will refuse, because a timer has no console.
awk '$1 == "robothor-backup" { print $3 }' /etc/crypttab

# 1. Gates first, while nothing is torn down.
cryptsetup isLuks /dev/disk/by-uuid/<uuid>           # ours, or stop
smartctl -d scsi -H /dev/sdX                         # FAILED → stop, replace

umount -l /mnt/robothor-backup                       # lazy: a clean umount hangs
dmsetup deps -o devno robothor-backup                # matches the device? live
lsblk -no MAJ:MIN /dev/disk/by-uuid/<uuid>           # ...compare with this
dmsetup info -c --noheadings -o open robothor-backup # MUST be 0 before any fsck

# 2. Live mapping (deps match) and open count 0 — just put it back.
mount /dev/mapper/robothor-backup /mnt/robothor-backup

# 3. Stale mapping (deps do NOT match). The stale name cannot be reused.
cryptsetup close robothor-backup                     # usually FAILS: kernel ref
cryptsetup open /dev/disk/by-uuid/<uuid> robothor-backup-1 --key-file <keyfile>
fsck.ext4 -p /dev/mapper/robothor-backup-1           # PREEN ONLY. rc>=4 → stop
mount /dev/mapper/robothor-backup-1 /mnt/robothor-backup   # the SAME path
```

The units bind to the **path**, not the mapper name, so nothing else needs
changing. `smartctl -d scsi -H /dev/sdX` is the health probe that works through
this USB bridge — `-d sat` returns nothing on it.

Re-read the open count immediately before the `fsck`, every time: `umount -l`
detaches the tree and returns, and the last reference is dropped whenever the
last user lets go. An `fsck` on a mapping the kernel is still handing out turns
a degraded volume into a corrupted one. The guard refuses at exactly that
point, and so should you.

## Known limitation: the guard defers, it does not prevent

`busy_backup_unit` asks `systemctl is-active` for each of the five backup units
and skips the tick if any is `activating`. That is a check followed by an
action, and a backup unit can start in the gap between them — the guard's own
timer and the backup timers are not coordinated. The window is small (the
`umount -l` follows within a second or so) and a lazy unmount under a running
job fails that job's run rather than corrupting the volume, but it is a real
race, not a prevented one.

The real fix is a lock shared with the backup scripts — `flock` on one file
that both the guard and every backup unit take — so that "a backup is running"
is a fact the guard holds rather than one it sampled. Follow-up; the guard's
own `${STATE_DIR}.lock` only serialises the guard against itself.

## Knobs

Set in `/etc/robothor/robothor.env`.

| Variable | Default | Meaning |
|---|---|---|
| `ROBOTHOR_BACKUP_MOUNT` | `/mnt/robothor-backup` | mount point the guard watches and remounts |
| `ROBOTHOR_VOLUME_GUARD_HEAL` | `1` | `0` pages but never touches the device |
| `ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS` | `86400` | quiet period between DOWN pages while it stays down |
| `ROBOTHOR_VOLUME_GUARD_MAPPER` | `robothor-backup` | the crypttab entry name |
| `ROBOTHOR_CRYPTTAB` | `/etc/crypttab` | where the container's UUID is resolved from |
| `ROBOTHOR_VOLUME_GUARD_STATE_DIR` | `/run/robothor/volume-guard` | `down_since` / `last_paged` / `heal_count`; tmpfs, so a reboot re-arms |
| `ROBOTHOR_BACKUP_STATE_DIR` | `/var/lib/robothor/backup-state` | last-good markers, on NVMe |

The guard exits 0 whatever the volume's state — a down volume is news, not a
unit failure. It exits 1 **only** when it cannot do its job: a page that could
not be delivered, a state directory it cannot create, or a missing
`scripts/backup-state.sh` (without it every "last good" line would go out
blank, which reads as reassuring). Each of those fires its own
`OnFailure=robothor-alert@%n.service`.

## Probe it — do not trust the silence

A guard nobody has fired is a guard nobody knows works. In a quiet window:

```bash
sudo systemctl start robothor-backup-volume-guard.service
journalctl -u robothor-backup-volume-guard.service -n 20 --no-pager
# → "backup-volume-guard: volume healthy at /mnt/robothor-backup"

# Now make it real. emergency_ro is not reachable on demand; read-only is.
sudo mount -o remount,ro /mnt/robothor-backup
sudo systemctl start robothor-backup-volume-guard.service
# → exactly ONE page, and the guard remounts it rw

sudo systemctl start robothor-backup-volume-guard.service
# → NO second page (the day-long repage window, or a recovery notice if the
#   remount worked)
```

If the first start produces no page, the control is inert — check the timer is
enabled (`systemctl list-timers 'robothor-backup*'`) and that
`ROBOTHOR_VOLUME_GUARD_HEAL`/the pager credentials are set.
