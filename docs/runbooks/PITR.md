# Point-in-Time Recovery (WAL archiving)

## What this bought

| | before | after |
|---|---|---|
| Recovery point | the nightly dump — **RPO 24h** | **~15 min** (offsite timer), ~5 min locally |
| Recovery granularity | "yesterday, 04:30" | **any second** since the base backup |
| Restore target | `pg_dump` only | physical base + WAL replay |

**You cannot PITR onto a `pg_dump`.** A logical dump is not a base backup. An
instance with WAL archiving and no `pg_basebackup` has an archive that restores to
nothing — and it looks exactly like a working setup until you need it.

## The pieces

| Piece | What it does |
|---|---|
| `archive_command` -> `wal-archive.sh` | copies each completed WAL segment to `$ROBOTHOR_WAL_ARCHIVE_DIR` — the scripts default to `/var/lib/postgresql/wal_archive`, and the shipped `robothor-wal-offsite.service` sets it to `/var/lib/robothor/wal_archive`. Both halves read the same variable; set it once, in the unit or the env file, or the shipper and the archiver look in different places |
| `archive_timeout = 5min` | forces a segment switch on an idle box, so the recovery point stays fresh |
| `robothor-basebackup.timer` (weekly) | `pg_basebackup` -> the encrypted volume. **This is what WAL replays onto.** |
| `robothor-wal-offsite.timer` (*/15) | replicates archive + base offsite, prunes spent WAL, **and checks archiving is not silently failing** |

## The thing that kills databases

**If `archive_command` keeps failing, Postgres does not drop the segment — it
retains it forever and retries.** `pg_wal` grows without bound until the filesystem
fills, and then **the database stops.** A broken archive command is a slow-motion
outage, and it is the most common way PITR takes production down.

Two rules follow, and the scripts obey them:

1. **Archive to a disk that does not disappear.** Not the USB backup volume — that
   disk physically dropped off the bus on 2026-07-14, mid-write. WAL goes to the
   NVMe, where the database already lives.
2. **Offsite replication is a separate timer**, never inside `archive_command`, so
   a network hiccup can never wedge the database.

`wal-offsite.sh` fails loudly (and pages, via `OnFailure`) if
`pg_stat_archiver.failed_count > 0` or if free space drops below 10GB. **Zero
archived segments is not evidence of health** — it is equally consistent with the
command being broken.

## Degraded mode: when the backup volume is wedged but the WAL still ships

The four other backup units carry an `ExecCondition=` and **skip** when the
encrypted USB volume is unusable. `robothor-wal-offsite` deliberately does
**not**. It runs every 15 minutes and that push *is* the 15-minute RPO; the WAL
archive lives on the NVMe, so refusing to run because a *different*,
USB-attached disk is wedged would trade a paging storm for real data loss.

So `wal-offsite.sh` calls the same probe
(`scripts/backup-volume-check.sh --ro $ROBOTHOR_BASEBACKUP_DIR`) itself and
degrades:

| | volume healthy | volume unhealthy (**degraded**) |
|---|---|---|
| Ship WAL offsite (`rclone copy` → `<remote>/wal`) | yes | **yes** |
| Replicate base backups → `<remote>/basebackup` | yes | skipped |
| Prune spent WAL (`pg_archivecleanup`) | yes | **skipped** |
| Stamp `last-wal-offsite-ok` | yes | **yes** — the WAL did go offsite, which is what the marker is about |
| Exit status | 0 | 0 |

Read the journal line, because a degraded run looks like a healthy one from
outside:

```
backup volume unhealthy — skipping basebackup replication and WAL prune
backup volume unhealthy — NOT pruning WAL (the prune horizon is read from the
newest base backup, which is unreadable)
```

Three things follow, and each is the safe direction to fail:

1. **Nothing is pruned while the volume is down.** The prune horizon is read
   from the newest `backup_label` on that volume, so when it is unreadable the
   horizon is unknowable. An over-eager prune destroys the ability to recover;
   an under-pruned archive is only a disk-space problem — and §4's disk guard
   still pages at <10GB free. The archive will grow for as long as the volume
   stays down, so a degraded run is a clock, not a steady state.
2. **A missing probe counts as unhealthy.** If `backup-volume-check.sh` is not
   executable at `$ROBOTHOR_VOLUME_CHECK`, the script degrades rather than
   assuming health — assuming health is what put us in the outage.
3. **Probe exit 255 degrades *and* fails.** 255 is the probe saying "I cannot
   answer the question" (its own tools are missing). Treating it here as "the
   volume is down" would leave this unit permanently degraded and permanently
   silent: no base-backup replication, no prune, and no failure to page about
   it. So 255 degrades and then exits non-zero, which pages once per
   `OnFailure=` cooldown instead of never.

**A degraded run still stamps `last-wal-offsite-ok`.** That is correct — the
WAL genuinely reached the remote — but it means the WAL marker cannot tell you
the base backups are stale. `last-basebackup` is the one that goes quiet, and
the consequence line for `*basebackup*` in `docs/runbooks/PAGING.md` is what
says so: *PITR must replay every WAL since <marker> — restore time growing
nightly*. Fix the volume (`docs/runbooks/BACKUP_VOLUME_GUARD.md`); the RPO is
fine, the RTO is what is drifting.

The marker is stamped only when `$ROBOTHOR_OFFSITE_REMOTE` is set **and** the
push succeeded. On an instance with no offsite destination nothing is
attempted, so nothing fails — and gating on the failure flag alone used to
stamp "the WAL is offsite" every 15 minutes on a box that had no offsite at
all.

## Two traps that will break your restore at 3am

Both of these were hit during the drill. Neither is obvious.

**1. On Debian/Ubuntu the config is NOT in the data directory.**
`postgresql.conf`, `pg_hba.conf` and `pg_ident.conf` live in `/etc/postgresql/16/main/`,
so `pg_basebackup` **does not include them**. A restored data directory will not
start:

```
postgres: could not access the server configuration file ".../postgresql.conf"
```

You must copy them in yourself — and create `conf.d/`, which Debian's config
`include_dir`s.

**2. The archived segments must be readable by whoever ships them offsite.**
Postgres writes them as `postgres`; rclone's credentials live in the operator's
home and `postgres` cannot read them. The archive dir is therefore `setgid`
(`chmod 2770`, group = the operator) so new segments inherit a readable group.
Without it the WAL is archived locally and **never leaves the box** — a backup that
does not survive losing the box, which is the one scenario it exists for.

## Restore to a point in time

```bash
TARGET='2026-07-14 14:02:42-04'          # any second since the base backup
BASE=$(sudo ls -1dt /mnt/robothor-backup/robothor/basebackup/base-*/ | head -1)
DATA=/var/lib/postgresql/restore

sudo install -d -o postgres -g postgres -m 0700 "$DATA"
sudo -u postgres tar -xzf "${BASE}base.tar.gz"   -C "$DATA"
sudo -u postgres bash -c "mkdir -p $DATA/pg_wal && tar -xzf '${BASE}pg_wal.tar.gz' -C $DATA/pg_wal"

# TRAP 1: the config is not in the backup.
sudo -u postgres cp /etc/postgresql/16/main/{postgresql,pg_hba,pg_ident}.conf "$DATA/"
sudo -u postgres mkdir -p "$DATA/conf.d"

sudo -u postgres tee -a "$DATA/postgresql.conf" <<EOF
port = 5499
archive_mode = off
data_directory = '$DATA'
hba_file = '$DATA/pg_hba.conf'
ident_file = '$DATA/pg_ident.conf'
restore_command = 'cp /var/lib/robothor/wal_archive/%f %p'
recovery_target_time = '$TARGET'
recovery_target_action = 'promote'
EOF
sudo -u postgres touch "$DATA/recovery.signal"

sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl -D "$DATA" -l /tmp/restore.log start -w
```

Confirm it stopped where you asked — **do not assume**:

```
LOG:  recovery stopping before commit of transaction NNNN, time 2026-07-14 14:02:45
LOG:  restored log file "0000000100000029000000DD" from archive
```

## Drill result (2026-07-14)

Rehearsed on this box, not theorised. Two markers were written either side of a
timestamp, then the archive was replayed to that timestamp:

```
BEFORE the target  -> present in the recovered DB   ✅
AFTER  the target  -> ABSENT from the recovered DB  ✅
crm_tasks 17,746 | memory_facts 139,069 | agent_runs 38,673   (intact)
```

Recovery of a ~4GB database took seconds; the base-restore-from-dump path was
previously measured at 9m01s (`RESTORE_DRILL.md`).

**Re-run this drill after any Postgres major upgrade.** A backup you have not
restored is not a backup.
