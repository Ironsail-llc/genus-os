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

## Setup

**This instance uses Google Drive**, reusing the Workspace OAuth credentials the
agent already holds (`~/.config/gws/credentials.json` — its refresh token carries
the `drive` scope). **No new credential is required.**

```bash
# one-time: create the rclone remote from the existing Google OAuth client
python3 - <<'EOF'
import json, urllib.parse, urllib.request, time, subprocess
d = json.load(open('/home/<user>/.config/gws/credentials.json'))
data = urllib.parse.urlencode({'client_id': d['client_id'], 'client_secret': d['client_secret'],
    'refresh_token': d['refresh_token'], 'grant_type': 'refresh_token'}).encode()
r = json.load(urllib.request.urlopen(urllib.request.Request(
    'https://oauth2.googleapis.com/token', data=data), timeout=20))
tok = {"access_token": r["access_token"], "token_type": "Bearer",
       "refresh_token": d["refresh_token"],
       "expiry": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z",
                               time.gmtime(time.time() + r.get("expires_in", 3600)))}
subprocess.run(["rclone", "config", "create", "gdrive", "drive",
    "client_id", d["client_id"], "client_secret", d["client_secret"],
    "scope", "drive", "token", json.dumps(tok)], check=True)
EOF

# then set, in the instance env:
#   ROBOTHOR_OFFSITE_REMOTE=gdrive:robothor-offsite
```

**GOTCHA**: do *not* set an empty `root_folder_id` in the rclone remote — rclone
then scans the entire Drive and the copy hangs with no output. Omit the key.

Any S3-compatible target (R2, B2, S3, MinIO) works identically — set
`ROBOTHOR_OFFSITE_REMOTE` to that rclone remote instead. (Cloudflare R2 was the
first choice, but this instance's `CLOUDFLARE_API_TOKEN` has no R2 scope, and
provisioning one is account-scoped; Drive needed nothing new.)

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
