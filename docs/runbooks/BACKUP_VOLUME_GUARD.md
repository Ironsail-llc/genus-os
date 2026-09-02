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

`scripts/backup-volume-check.sh` is what tells the difference. It is wired as
`ExecCondition=` on the four backup units — `robothor-backup-local`,
`robothor-backup-offsite`, `robothor-backup-verify` and `robothor-basebackup` —
and its exit code is read by systemd:

| Exit | systemd's reading | Effect |
|---|---|---|
| `0` | the condition holds | the unit runs |
| `1`–`254` | the condition does **not** hold | the unit is **skipped**: `Result=exec-condition`, `OnFailure=` does **not** fire, no page |
| `255` | the check itself failed | the unit **fails** and `OnFailure=` **does** fire |

`255` is reserved for "this probe cannot answer the question" — its own tools
(`timeout`, `findmnt`) are missing. **Every** "the volume is not healthy"
answer is `1`, on purpose: a wedged disk should make the backups skip quietly
rather than page four times an hour with no new information. That is what
stopped the storm (96 `robothor-wal-offsite` failures a day, ~22 pages whose
entire content was a unit name).

The probe is also called in-script by `backup-ssd.sh`, `pg-basebackup.sh` and
`wal-offsite.sh`, so the guarantee holds however the script is invoked — not
only when systemd is the caller. `wal-offsite.sh` is the one that **degrades
instead of skipping**: the WAL archive is on NVMe and that push *is* the 15-min
RPO, so it skips only the two steps that read the backup volume (replicating
the base backups, and reading the newest `backup_label` to fix the prune
horizon) and still ships the WAL. See `docs/runbooks/PITR.md`.

A skipped unit fires no `OnFailure=`, so on its own that fix trades a storm for
silence.

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
| `mapper robothor-backup still has N opener(s) after umount -l — refusing to fsck a referenced mapping; the volume is now UNMOUNTED and the next tick remounts it once the holder lets go` | The mapping is the device's own and correct, but something still holds it: `umount -l` returns before the last reference is dropped. `fsck.ext4 -p` there would corrupt a filesystem that was only degraded. **The lazy unmount already happened**, so the volume is no longer at the path — it is not degraded now, it is absent, and it stays absent while the holder holds. | `lsof +f -- /dev/mapper/robothor-backup` / `fuser -vm /mnt/robothor-backup` to find the holder (a login shell sitting in the directory counts) and stop it. The next tick then remounts it; nothing else is needed. If nothing is holding it, the reference is a kernel one: reboot. |
| `something other than the backup mapper is mounted at /mnt/robothor-backup (SOURCE) — refusing to unmount it` | The probe says the path is unusable and what is mounted there is not one of this guard's mapper nodes at all. Somebody mounted over the mountpoint. The guard touched nothing: `umount -l` names a path, and unmounting a stranger's filesystem out from under its users is not this control's job. | Find out what `SOURCE` is (`findmnt /mnt/robothor-backup`) and unmount it yourself when whatever is using it is done. The next tick then heals the real volume. |
| `something other than the backup mapper is mounted at /mnt/robothor-backup (SOURCE is not backed by DEVICE) — refusing to unmount it` | A node **named** like this guard's mapper is mounted there, but it is neither backed by our device nor a mapping of our LUKS container — see "How it decides what is ours". A name is not an identity. | `dmsetup deps <name>` and `dmsetup info -c -o uuid <name>` say what it really is. Unmount it yourself once whatever is using it is done. |
| `could not ask findmnt what is mounted at /mnt/robothor-backup (exit N) — refusing to guess` | findmnt failed rather than answering. `findmnt` exits non-zero both for an empty mountpoint and for a real error, so an exit it cannot explain is never read as "nothing is mounted there" — everything the heal does is aimed at whatever is really at the path. The guard touched nothing. | Check `findmnt` works at all (`findmnt /mnt/robothor-backup`; exit 127 means util-linux is missing from the unit's PATH). The next tick heals it once findmnt answers. |
| `N filesystems stacked at /mnt/robothor-backup (SOURCES) — refusing to unmount any of them` | More than one filesystem is mounted at the path. `umount -l` pops the TOP of the stack, which is not the layer the identity check can vouch for, so the guard will not touch any of them. | `findmnt -o SOURCE,FSTYPE /mnt/robothor-backup` lists the layers. Unmount whatever was stacked on top once its user is done; the next tick heals the real volume. |
| `no non-interactive keyfile in crypttab (column 3 = X); refusing to tear down a mapping I cannot rebuild` | crypttab column 3 is `none`, `-`, or a file root cannot read, so `cryptsetup open` would prompt on a console the timer does not have. The guard did **not** unmount or close anything — it left the volume degraded rather than making it absent. | Put a readable keyfile in column 3 of `/etc/crypttab` (`cryptsetup luksAddKey` first), or run the manual procedure below and unlock it by hand. |
| `... heal deferred: <unit> is activating` | A backup unit was mid-run; unmounting under it would corrupt the backup. | Nothing — the next tick heals it. |
| `<mount> is mounted emergency_ro ...` with `HEAL=0` | Healing is switched off. | Re-enable, or run the manual procedure below. |

## How it decides what is ours

By the **device**, not by the name. `/dev/mapper/robothor-backup[-<token>]` is
only the cheap first filter — it says which node to ask about, and anything
with root can create a node under that spelling. A node is this guard's when
either `dmsetup deps` resolves to the same `MAJ:MIN` as the crypttab
`UUID=` → `/dev/disk/by-uuid` → `lsblk` chain (the device's own **live**
mapping), or its dm-crypt UUID — `CRYPT-LUKS2-<container uuid>-<name>` — names
our LUKS container (our own **corpse**, after a drop has taken the deps away;
an orphaned `error` target depends on nothing, which is the 2026-08-27
signature). Only the container field of that UUID counts: the `<name>` after it
is a string whoever made the node chose, so a node merely *named* after our
container is not ours. Anything else at the mountpoint is somebody's filesystem
and the guard refuses to touch it.

When crypttab names the device as a **path** instead of `UUID=<…>`, the file
carries no container UUID and the corpse half of that test would have nothing
to compare against — so the guard reads it from the header with `cryptsetup
luksUUID` (read-only). If even that fails it logs `node identity is degraded to
deps-only`, which is the state in which it cannot recognise its own corpse and
will refuse the heal the drop signature needs.

The same question picks the mapping to reuse. Every node wearing one of those
names is asked, not just the bare one — after a heal the live mapping is
`robothor-backup-1`, and a hand recovery may have left one under some other
token (`robothor-backup-<token>`), which the glob still reaches. If any of them
is backed by the device it is mounted back
as it stands (no key, no `cryptsetup open`, no `fsck`); if one is backed but
held, the guard refuses and names the holder rather than stacking a second LUKS
mapping over the same disk. A fresh name is opened **only** when no node is
backed by the device at all.

## The manual procedure

Exactly what the guard automates. Run it as root when the guard is disabled or
when you want to watch it happen.

```bash
# 0. Stop the timer first — see "While the guard timer is enabled, do NOT"
#    below. A tick landing between two of these commands does its own heal on
#    top of yours. Start it again when you are done.
sudo systemctl stop robothor-backup-volume-guard.timer
systemctl is-active robothor-backup-volume-guard.service   # "inactive": nothing mid-flight

# 1. Can you get back in? crypttab column 3 must be a keyfile you can read;
#    `none` or `-` means the guard will refuse, because a timer has no console.
awk '$1 == "robothor-backup" { print $3 }' /etc/crypttab

# 2. Gates first, while nothing is torn down.
cryptsetup isLuks /dev/disk/by-uuid/<uuid>           # ours, or stop
smartctl -d scsi -H /dev/sdX                         # FAILED → stop, replace

findmnt -rn -o SOURCE --mountpoint /mnt/robothor-backup  # /dev/mapper/robothor-backup[-<token>]
                                                     # ...or somebody else's: stop
dmsetup info -c --noheadings -o uuid <that name>     # CRYPT-LUKS2-<our uuid>-… or stop
umount -l /mnt/robothor-backup                       # lazy: a clean umount hangs
lsblk -no MAJ:MIN /dev/disk/by-uuid/<uuid>           # the device: compare deps with this
for n in /dev/mapper/robothor-backup /dev/mapper/robothor-backup-*; do
  dmsetup deps -o devno "${n##*/}"; done             # ANY node matching = the live one
dmsetup info -c --noheadings -o open <that name>     # MUST be 0 before any fsck

# 3. Live mapping (deps match, under WHATEVER name) and open count 0 — just put
#    it back. Never open a second mapping over a device that has a live one.
mount /dev/mapper/<that name> /mnt/robothor-backup

# 4. No node matches the device. The stale name cannot be reused.
cryptsetup close robothor-backup                     # usually FAILS: kernel ref
cryptsetup open /dev/disk/by-uuid/<uuid> robothor-backup-1 --key-file <keyfile>
fsck.ext4 -p /dev/mapper/robothor-backup-1           # PREEN ONLY. rc>=4 → stop
mount /dev/mapper/robothor-backup-1 /mnt/robothor-backup   # the SAME path

# 5. Re-arm the guard. Not optional: from here until this runs, nothing is
#    watching the volume.
sudo systemctl start robothor-backup-volume-guard.timer
systemctl list-timers 'robothor-backup*' --no-pager
```

The units bind to the **path**, not the mapper name, so nothing else needs
changing. `smartctl -d scsi -H /dev/sdX` is the health probe that works through
this USB bridge — `-d sat` returns nothing on it.

Re-read the open count immediately before the `fsck`, every time: `umount -l`
detaches the tree and returns, and the last reference is dropped whenever the
last user lets go. An `fsck` on a mapping the kernel is still handing out turns
a degraded volume into a corrupted one. The guard refuses at exactly that
point, and so should you.

## Is the disk dying, or is the bridge?

A drop that keeps coming back is one of two things, and they have opposite
fixes. The guard answers neither — it only refuses to remount a disk SMART
calls `FAILED` (`smart_gate`) and counts the drops it heals. The rest is a
human with the disk in front of them.

**Ask SMART first.** Through this class of USB enclosure `-d sat` returns
nothing and `-d scsi` works — that is why the guard uses `-d scsi`, and why a
`smartctl` that is not installed disables the gate rather than blocking the
heal:

```bash
smartctl -d scsi -H    /dev/sdX     # the gate the guard runs: PASSED / FAILED
smartctl -d scsi -A    /dev/sdX     # attributes, if the bridge passes them through
smartctl -d sat  -A    /dev/sdX     # try both: many bridges answer only one
```

**Then read the surface.** SMART health is a summary and a summary can say
`PASSED` over a disk that cannot return its own blocks. A whole-device
sequential read is the cheap falsifier — it is read-only, it touches nothing,
and it can be run on the *unlocked mapper* (which reads the plaintext) or on
the raw device:

```bash
# Read-only, no writes, no mount required. Takes as long as the disk is big.
dd if=/dev/sdX of=/dev/null bs=8M status=progress conv=noerror,sync
dmesg -T | tail -60      # the errors land here, not in dd's summary
```

**The stop rule, and it is a stop rule, not a judgement call:**

1. **Any read error, any `I/O error` in `dmesg`, or any reallocated/pending
   sector count above zero and rising → stop. Do not `fsck`, do not remount,
   do not write.** An `fsck` writes; a write to a failing disk is how a
   recoverable volume becomes an unrecoverable one, and this volume's whole
   job is to be recoverable.
2. **Verify the offsite copy before you touch the local one** —
   `docs/runbooks/OFFSITE_BACKUP.md`, then `docs/runbooks/RESTORE_DRILL.md`.
   The order matters: the moment you have a verified offsite generation, every
   subsequent decision about the local disk is cheap.
3. Only a **clean** surface read plus a `PASSED` SMART health makes the disk a
   candidate for `fsck.ext4 -y` and further service. Anything else is the
   replace path below.

A clean surface read with repeated drops points at the **bridge, cable or
port**, not the disk — see below.

## When the drop keeps coming back: the USB bridge

`⚠️ backup volume auto-recovered (USB drop #N since boot; ...)` carries `#N`
for exactly this. The guard's `heal_count` lives on tmpfs
(`ROBOTHOR_VOLUME_GUARD_STATE_DIR`, `/run/...`), so `#N` is **drops since
boot** — and two or more in a day, with a clean surface read, is a failing
bridge, cable or port, not a fluke and not a dying disk.

In escalating order, cheapest first:

```bash
# 1. Which port and which bridge is it actually on?
lsusb -t                                  # the tree: which controller, which speed
dmesg -T | grep -iE 'usb|uas|reset|disconnect' | tail -40
lsusb                                     # note the enclosure's <vid>:<pid>
```

1. **Reseat, then move.** A different cable, then a different port — prefer one
   on a different controller in `lsusb -t`. A powered hub is worth trying: a
   drop under write load is often a power problem, not a data one.
2. **Disable UAS for that enclosure.** Many USB-SATA bridges advertise UAS
   (USB Attached SCSI) and implement it badly; the symptom is exactly this
   one — resets and disconnects under sustained write, clean when idle.
   Forcing the enclosure back to the plain `usb-storage` BOT transport costs
   throughput and buys stability:

   ```bash
   # <vid>:<pid> from `lsusb`; the trailing `u` means "ignore UAS for this device"
   # Test it for one boot first:
   #   add   usb-storage.quirks=<vid>:<pid>:u   to the kernel command line
   # Then make it permanent, e.g.:
   echo 'options usb-storage quirks=<vid>:<pid>:u' \
       | sudo tee /etc/modprobe.d/robothor-backup-usb.conf
   sudo update-initramfs -u        # the module loads from the initramfs
   # Reboot, then confirm the device came up on usb-storage and not uas:
   lsusb -t | grep -i 'storage\|uas'
   ```

   **This is a host-level mitigation. Nothing in this repo sets it, checks it,
   or knows about it** — if you apply it, it is yours to record in the
   instance's own notes, because a rebuilt box comes up without it.
3. **Replace the enclosure** before you replace the disk. It is the cheaper
   half and, on a clean surface read, the likelier culprit.

## Replacing the drive

The order is fixed, and step 1 is not optional.

```bash
# 1. PROVE you can restore from offsite BEFORE you unplug anything.
#    docs/runbooks/OFFSITE_BACKUP.md (verify) then docs/runbooks/RESTORE_DRILL.md
#    (restore a generation into a scratch DB). A replacement plan that starts
#    by destroying the only local copy is not a plan.

# 2. Stop the guard, so a tick cannot act on a half-swapped volume.
sudo systemctl stop robothor-backup-volume-guard.timer
systemctl is-active robothor-backup-volume-guard.service   # "inactive"

# 3. Stop and disable the four backup timers, so none of them fires mid-swap.
sudo systemctl stop robothor-backup-local.timer robothor-backup-offsite.timer \
                    robothor-backup-verify.timer robothor-basebackup.timer

# 4. Format the new disk as a LUKS container and give it a keyfile.
sudo cryptsetup luksFormat /dev/sdY
sudo cryptsetup luksAddKey /dev/sdY <keyfile>          # non-interactive unlock
sudo cryptsetup open /dev/sdY robothor-backup --key-file <keyfile>
sudo mkfs.ext4 -L robothor-backup /dev/mapper/robothor-backup

# 5. Point crypttab and fstab at the NEW container, BY UUID.
sudo cryptsetup luksUUID /dev/sdY                      # <uuid> for crypttab col 2
#   /etc/crypttab: robothor-backup  UUID=<uuid>  <keyfile>  luks
#   Column 3 MUST be a keyfile root can read. `none` or `-` means the guard
#   refuses to tear anything down, because a timer has no console to prompt on.
#   /etc/fstab: the SAME mount point as before — the units bind to the PATH,
#   never to the mapper name, so nothing else changes.
sudo systemctl daemon-reload
sudo mount <mount>

# 6. Recreate the directory layout the units expect, then let them refill it.
#    robothor-backup-local (backup-ssd.sh) creates the gated paths; the other
#    three ExecCondition= on directories it makes, so run it first.
sudo systemctl start robothor-backup-local.service
sudo systemctl start robothor-basebackup.service

# 7. Re-enable everything, guard LAST — and check it.
sudo systemctl start robothor-backup-local.timer robothor-backup-offsite.timer \
                     robothor-backup-verify.timer robothor-basebackup.timer
sudo systemctl start robothor-backup-volume-guard.timer
systemctl list-timers 'robothor-*backup*' --no-pager

# 8. Confirm the markers move. Until these are fresh, the swap is not finished:
#    an operator reading a page still sees the OLD generation as "last good".
cat /var/lib/robothor/backup-state/last-local-dump
cat /var/lib/robothor/backup-state/last-basebackup
cat /var/lib/robothor/backup-state/last-offsite-ok
```

The markers in step 8 are on **NVMe**, not on the volume you just replaced —
they survive the swap, which is why a stale one after it is a real finding and
not an artefact.

## Incident log

Kept here because the reason table above is the *what* and this is the *how
often*. `#N` in a recovery page is drops since boot; this is drops since the
runbook.

| Date | What happened | Outcome |
|---|---|---|
| 2026-07-14 | First recorded drop. ext4 flipped to `emergency_ro`; every `stat()` still succeeded, so `mountpoint -q` and `[[ -d ]]` passed and the backup units ran against a dead disk. | Recovered by hand. No guard existed. |
| 2026-08-24 | Second drop. The restore drill's dump glob matched nothing and the pipeline "succeeded" in 0.09s against an empty database. | Recovered by hand; the drill grew an empty-`$DUMP` abort (`docs/runbooks/RESTORE_DRILL.md`). |
| 2026-08-26 – 27 | Third drop, and the long one. `robothor-wal-offsite` failed every 15 minutes — 96 failures in a day, ~22 pages whose entire content was a unit name. A page that fires 96 times for one unfixed condition is a muted pager. | Recovered by hand. The stale device-mapper node held a kernel reference nothing in userspace could release, so the recovery had to open the container under a **new** mapper name. |
| 2026-08-31 | Not a drop: the pager's own outage. 63 `curl_rc=6` (`Could not resolve host`) lines in the journal — callers with no retrying unit behind them lost their pages outright. | `docs/runbooks/PAGING.md`: the durable spool. |
| 2026-09-02 | Recovery and hardening. The volume was brought back and the three defects below were closed. | `scripts/backup-volume-check.sh` + `ExecCondition=`, `scripts/backup-volume-guard.sh` + its timer, `scripts/backup-state.sh` markers, and the consequence map in `scripts/send_failure_alert.sh`. |

### The three defects the log actually turned up

Each of these was a control that existed, ran, and reported success.

1. **`-d` on an `emergency_ro` mount.** Every guard in the backup chain was a
   `stat()` guard — `mountpoint -q` in `backup-ssd.sh` and `pg-basebackup.sh`,
   `[[ -d ]]` in `wal-offsite.sh`. `emergency_ro` breaks `readdir()` and
   `write()` and leaves `stat()` working, so all of them passed while the disk
   was gone. The fix is a probe that does a **real `readdir`**, and a real
   write for `--rw` callers — `scripts/backup-volume-check.sh` §4 and §5. This
   is why the probe is bounded by `timeout` at every step: a device off the bus
   can block `stat()` too, and an unbounded probe leaves the unit in
   `activating` until `TimeoutStartSec` (3600s for the nightly backup), which
   is worse than the failure it replaced.

2. **A one-hour cooldown against a fifteen-minute timer.** The sender dedups
   per key for `ROBOTHOR_ALERT_COOLDOWN_SECONDS` (default 3600). With
   `robothor-wal-offsite` failing every 15 minutes, that still let roughly one
   page an hour through for a condition nobody could act on any faster —
   96 failures a day, ~22 pages, all identical, all just a unit name. A
   cooldown is the wrong instrument for a condition that will not clear on its
   own. The fix is not a longer cooldown: it is `ExecCondition=` making the
   unit **skip** (no `OnFailure=`, no page at all), plus a separate guard that
   pages **once per heal, or once a day** while the volume stays down
   (`ROBOTHOR_VOLUME_GUARD_REPAGE_SECONDS`), plus the last-good markers so the
   one page that does arrive says what was actually lost.

3. **A stale mapper node that only a reboot clears.** After the drop, the
   device-mapper node kept a kernel reference: `cryptsetup close` failed, and
   the name could not be reused. The guard therefore never reuses a burned
   name — it opens under the first free `<mapper>-1` … `<mapper>-9` and mounts
   at the **same path**, because the units bind to the path and not to the
   mapper name. When all nine alternates are burned it stops and says
   `9 stale mappings, reboot required`, which is the honest answer: nothing in
   userspace can clear nine kernel references.

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

## While the guard timer is enabled, do NOT

The timer runs as root every 10 minutes and its whole job is to act on that
mountpoint. Anything you do to the volume by hand is racing it, and the guard
does not know you are there.

1. **Do not mount anything by hand at `ROBOTHOR_BACKUP_MOUNT`**
   (`/mnt/robothor-backup`). A live timer probes that path, and something it
   cannot use gets lazy-unmounted within 10 minutes. The guard refuses when the
   mounted source is not its own mapper — `something other than the backup
   mapper is mounted at <mount> (<source>) — refusing to unmount it` — but that
   refusal is a page and a stalled heal, not permission to park a filesystem
   there. Mount it somewhere else.

2. **Do not leave a shell or a process with its cwd, or an open file, inside
   the volume.** That is an opener on the mapper, and the guard will not fsck a
   referenced mapping: it refuses with `refusing to fsck a referenced mapping`.
   By then it has already lazy-unmounted the volume, so the backups stay down
   until you let go. `cd` out of the tree before you walk away, and check with
   `fuser -vm /mnt/robothor-backup` if a heal keeps refusing.

3. **Stop the timer before you work on the volume yourself, and start it
   again afterwards** — both before the manual procedure above and before the
   `mount -o remount,ro` drill below:

   ```bash
   sudo systemctl stop robothor-backup-volume-guard.timer
   systemctl is-active robothor-backup-volume-guard.service   # "inactive": no tick mid-flight
   ...                                                        # your work here
   sudo systemctl start robothor-backup-volume-guard.timer
   ```

   Stopping the timer does not stop a tick already running, which is what the
   second line is for; a heal in flight can take minutes (fsck). Without this,
   a tick can land between your `cryptsetup open` and your `fsck`, and — in the
   drill — will heal the volume you just broke before you have looked at it,
   which measures the guard racing itself rather than the control being tested.

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
# 0. Take the timer out of the loop FIRST. Every step below starts the service
#    by hand; a scheduled tick landing in between heals the volume you just
#    broke, and the drill then measures a race instead of the control.
sudo systemctl stop robothor-backup-volume-guard.timer
systemctl is-active robothor-backup-volume-guard.service   # "inactive": nothing mid-flight

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

# Last: the drill is NOT over until the timer is back. Check it — an unarmed
# guard is the failure this whole unit exists to prevent.
sudo systemctl start robothor-backup-volume-guard.timer
systemctl list-timers 'robothor-backup*' --no-pager
```

If the first start produces no page, the control is inert — check the timer is
enabled (`systemctl list-timers 'robothor-backup*'`) and that
`ROBOTHOR_VOLUME_GUARD_HEAL`/the pager credentials are set.
