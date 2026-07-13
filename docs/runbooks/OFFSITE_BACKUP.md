# Offsite Backup

## Why

Every backup this instance takes lives on a LUKS SSD **plugged into the same
machine as production**. One fire, theft, flood, or PSU surge takes production
*and* every backup with it. The local backup is a fast-recovery convenience;
it is not disaster recovery.

`scripts/backup-offsite.sh` replicates the **recoverable core** to an offsite
remote:

| What | Why it must survive the box |
|------|-----------------------------|
| `robothor_memory` dumps (~1.1 GB/night gz) | The database *is* the instance — memory, CRM, runs, audit |
| `robothor-engine.service.d/*.conf` | **The guardrail posture lives in `/etc`, not git.** Lose it and you lose which controls were enforcing |
| Instance config | Identity, agent manifests are user-land and not in the platform repo |

The 160 GB of images and Ollama models on the SSD are deliberately **not**
replicated — they are re-derivable and would dominate cost. Recovery of those
is a re-download, not a data loss.

## Setup (one credential needed)

The instance's existing `CLOUDFLARE_API_TOKEN` is valid but **has no R2 scope**
(verified: R2 endpoints return `Authentication error`), so it cannot create the
bucket or its keys. Provision one of:

**Cloudflare R2** (recommended — same account, 10 GB free tier covers ~7
generations of the DB dump):
1. Cloudflare dashboard → R2 → Create bucket `robothor-offsite`.
2. R2 → Manage API tokens → Create token with **Object Read & Write** on it.
3. Add to the instance secrets (SOPS, `secrets.enc.json`):
   ```
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   ROBOTHOR_OFFSITE_REMOTE=r2:robothor-offsite
   ```
4. Configure the rclone remote:
   ```bash
   rclone config create r2 s3 provider=Cloudflare \
       access_key_id="$R2_ACCESS_KEY_ID" \
       secret_access_key="$R2_SECRET_ACCESS_KEY" \
       endpoint="https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
   ```

Any S3-compatible target (B2, S3, MinIO) works identically — set
`ROBOTHOR_OFFSITE_REMOTE` to the rclone remote name.

## Install

```bash
sudo cp infra/systemd/robothor-backup-offsite.* /etc/systemd/system/
sudo cp infra/systemd/robothor-backup-verify.*  /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/robothor-backup-offsite.service   # fix paths/user
sudo systemctl daemon-reload
sudo systemctl enable --now robothor-backup-offsite.timer robothor-backup-verify.timer
```

- **Nightly 05:30** — replicate (after `backup-ssd.sh` at 04:30).
- **Weekly Sun 06:30** — verify the offsite copy still matches the source.
- Both units page the operator on failure via `robothor-alert@` (see
  `docs/runbooks/PAGING.md`). **A backup that silently stops running is the
  entire risk** — that is why failure is loud.

## Verify by hand

```bash
# dry run against a scratch local remote — proves the pipeline without cloud
ROBOTHOR_OFFSITE_REMOTE=/tmp/offsite-test \
ROBOTHOR_OFFSITE_SOURCE=/mnt/robothor-backup/robothor/db \
  bash scripts/backup-offsite.sh

# check the real remote is intact
ROBOTHOR_OFFSITE_VERIFY_ONLY=1 bash scripts/backup-offsite.sh
```

## Restore

Pull a generation down, then follow `docs/runbooks/RESTORE_DRILL.md` (measured
2026-07-13: 1.1 GB dump → working scratch DB in **9m01s, 0 errors, 92/92
tables**). The drop-ins come back from `<remote>/systemd/`.

```bash
rclone copy "$ROBOTHOR_OFFSITE_REMOTE/db/robothor_memory-YYYYMMDD.sql.gz" /tmp/
```

## Retention

`ROBOTHOR_OFFSITE_KEEP` (default 7) generations. At ~1.1 GB/night that is
~7.7 GB — inside R2's 10 GB free tier.
