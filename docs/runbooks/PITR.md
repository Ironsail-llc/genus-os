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
| `archive_command` -> `wal-archive.sh` | copies each completed WAL segment to `/var/lib/robothor/wal_archive` |
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
