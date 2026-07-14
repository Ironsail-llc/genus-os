# Genus OS Snapshot and Restore Runbook

This runbook defines the first supported disaster-recovery contract for a
self-hosted Genus OS instance. The `robothor snapshot` command captures a
PostgreSQL custom-format dump and the instance-owned workspace state needed to
reconstruct the entity.

The target service objectives are:

- RPO: 15 minutes. Run snapshots at least every 15 minutes for workloads that
  require this objective, and replicate them to independent storage.
- RTO: 60 minutes. Rehearse the complete restore procedure against a separate
  instance until it consistently completes inside this window.

A successful command is not, by itself, proof that either objective is met.
Monitor snapshot age, verify every replicated copy, and perform a documented
restore exercise at least quarterly.

## Recovery contract

Each snapshot has a versioned `manifest.json` containing:

- UTC creation time and snapshot-format version;
- Genus OS application version and instance ID/name;
- PostgreSQL database and custom-dump metadata;
- the applied canonical migration IDs and checksums;
- every included workspace file, its mode, size, and SHA-256 checksum;
- artifact sizes/checksums and explicit encryption/secret-bearing flags.

Workspace state currently includes these paths when they exist:

- allowlisted `.robothor` configuration, install, migration, initialization,
  federation configuration, override, archive, and scheduler state;
- `docs/agents`, `docs/workflows`, `docs/hooks`, and `docs/webhooks.yaml`;
- `agents/skills`;
- `brain`.

`ROBOTHOR_MANIFEST_DIR` and `ROBOTHOR_WORKFLOW_DIR` overrides are also
captured when they resolve inside the workspace. Snapshot creation fails
closed when either configured state directory is missing, symlinked, or
outside the workspace; move durable entity state under the mounted workspace
before relying on this recovery contract.

Only files beneath `ROBOTHOR_WORKSPACE` are included. A host-level
`~/.robothor/owner.yaml` used outside that workspace must be provisioned and
protected separately; the similarly named workspace file is captured only
when it exists.

Symlinks are rejected rather than followed. Environment files, provider
credentials, and arbitrary files outside this allowlist are not copied.
`.vault-key` and an existing federation identity bundle
(`.robothor/identity.json` plus `identity.key`) are included only with
`--include-secrets`.

## Encryption and key custody

Snapshots are encrypted by default with AES-256-GCM. The key is derived with
scrypt from a passphrase supplied through an environment variable; a passphrase
is never accepted on the command line, where it could be exposed by process
inspection or shell history.

Store the passphrase in the deployment's secret manager, independently from the
snapshot repository:

```bash
export GENUS_SNAPSHOT_PASSPHRASE="$(secret-manager read genus/snapshot-passphrase)"
robothor snapshot create
unset GENUS_SNAPSHOT_PASSPHRASE
```

`--plaintext` is an explicit escape hatch for non-secret test data. It produces
an unauthenticated archive and should not be used for production databases,
personal data, PHI, payment tokens, or `brain` state. A snapshot containing
workspace key material can never be written in plaintext.

Back up the snapshot passphrase under the organization's break-glass procedure.
Losing both the live passphrase and its escrowed copy makes encrypted snapshots
unrecoverable. Do not store the passphrase beside the snapshots.

## Create and inspect snapshots

The default repository is
`$ROBOTHOR_WORKSPACE/.robothor/snapshots`. Override it with
`GENUS_SNAPSHOT_REPOSITORY` or `--repository`.

```bash
# PostgreSQL + workspace, encrypted (default)
robothor snapshot create

# Also capture the vault master key and federation key material; encryption is mandatory
robothor snapshot create --include-secrets

# Database-only or workspace-only recovery points
robothor snapshot create --skip-workspace
robothor snapshot create --skip-database

# Automation-friendly inventory; no decryption is required
robothor snapshot list
```

`pg_dump` receives connection values as separate subprocess arguments and the
password only through `PGPASSWORD`; no shell is invoked. The finished snapshot
is permissioned `0600` and published atomically. Existing output is never
replaced unless an operator supplies both an exact `--output` and `--force`.
Creation and verification use private staging directories on the repository or
local temporary filesystem; capacity planning should allow multiple times the
uncompressed database/workspace size during these operations.

Encrypted operations keep plaintext intermediates in a mode-`0700` staging
directory and ciphertext publication state beside the destination. For
regulated workloads, set `GENUS_SNAPSHOT_STAGING_DIR` to a sufficiently sized
encrypted local volume or tmpfs; the command fails if the configured path is
missing or symlinked. Secure deletion cannot be guaranteed on SSD, copy-on-write,
or remote filesystems, so staging-volume encryption is part of the deployment
control.

Immediately verify each local and replicated copy:

```bash
robothor snapshot verify /backups/genusos-snapshot-instance-TIMESTAMP.gss
```

Verification decrypts/authenticates the snapshot, validates all recorded sizes
and checksums, inspects every workspace archive path, asks `pg_restore --list`
to validate the database dump, and checks application/schema compatibility. A
nonzero exit means the copy must not be used without investigation.

## Retention

Retention operates only on automatically named `genusos-snapshot-*` files, so
it will not delete unrelated archives. It is a dry run unless `--confirm` is
present:

```bash
# Keep the newest 96 snapshots and select older excess snapshots
robothor snapshot prune --keep 96 --older-than-days 2

# Review the output, then apply it
robothor snapshot prune --keep 96 --older-than-days 2 --confirm
```

The local retention policy is not an immutable/off-site backup policy. Copy
snapshots to storage with versioning or object lock, use a separate failure
domain and account, and verify the destination copy before pruning the source.

## Restore rehearsal (safe default)

Restore is verification-only by default. It changes neither PostgreSQL nor the
workspace:

```bash
robothor snapshot restore /backups/genusos-snapshot-instance-TIMESTAMP.gss
```

The plan reports workspace conflicts and compatibility warnings. Select a
single recovery domain when useful:

```bash
robothor snapshot restore SNAPSHOT --database-only
robothor snapshot restore SNAPSHOT --workspace-only --workspace /srv/genus-rehearsal
```

Use this dry-run path in scheduled recovery tests. A meaningful rehearsal then
restores into an isolated database/workspace, runs migration status and service
readiness checks, and samples critical entity workflows.

## Destructive restore

Before a production restore:

1. Declare a maintenance window and stop every Genus OS process that can write
   to PostgreSQL or the workspace.
2. Preserve the current failed state with a separate snapshot when possible.
3. Verify the chosen recovery point and its independent/off-site copy.
4. Confirm the target database and workspace configuration. Database credentials
   always come from the current `ROBOTHOR_DB_*` settings, not from the snapshot.
5. Execute and capture the command output in the incident record.

An actual restore requires `--confirm`. PostgreSQL restore uses `--clean` and
therefore additionally requires `--force`; workspace conflicts also require
`--force`:

```bash
robothor snapshot restore SNAPSHOT --confirm --force
```

The database restore runs through `pg_restore --single-transaction
--exit-on-error --clean --if-exists`, so a database error rolls back that
transaction. Workspace files are individually written to temporary files,
fsynced, and atomically replaced. The restore never deletes unrelated workspace
files. When a federation identity bundle is restored into a different workspace,
its verified `private_key_ref` is atomically relocated to the restored key path.

Afterward:

1. Run `robothor migrate --check` and apply any forward migrations required by
   the installed release.
2. Start services and require readiness, not only process liveness.
3. Validate operator authentication, agent fleet loading, vault access, a
   read/write memory transaction, and a representative workflow.
4. Create and verify a new post-recovery snapshot.
5. Record achieved RPO/RTO and any corrective actions.

## Compliance boundary

The database dump may contain regulated customer data even when no vault key is
included. Treat every production snapshot at the highest data classification of
the source database. Encryption, checksums, least-privilege storage, retention,
access logging, and tested deletion procedures are deployer responsibilities.

For payment workflows, Genus OS should retain provider tokens and the minimum
metadata needed for operations—not raw PAN, CVV/CVC, or equivalent authentication
data. Snapshot support does not change that rule and does not itself establish
PCI DSS, HIPAA, or other certification.
