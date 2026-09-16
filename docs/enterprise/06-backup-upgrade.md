# 6. Backup, restore and upgrade

Goal: a snapshot you have actually restored, and an upgrade you have already
rehearsed somewhere that is not production.

A backup nobody has restored is a hypothesis.

## Snapshots

A snapshot is the database and the workspace together, atomically, in one
authenticated file. Not a `pg_dump` and a `tar` you have to keep in step.

```bash
genus snapshot create
genus snapshot list
genus snapshot verify ./genusos-snapshot-instance-TIMESTAMP.gss
genus snapshot restore ./genusos-snapshot-instance-TIMESTAMP.gss
genus snapshot restore ./genusos-snapshot-instance-TIMESTAMP.gss --confirm --force
genus snapshot prune --keep 7 --older-than-days 30 --confirm
```

Four things about that list are deliberate:

- **`verify` is a real check**, not a file-exists test: it authenticates the
  archive, checksums it, inspects its contents and checks the platform version
  it came from against the one you are standing on.
- **`restore` is a dry run by default.** It verifies and prints what it would
  do; `--confirm` executes and `--force` authorises the destructive parts —
  cleaning the database, replacing the workspace. Two flags, because those are
  two decisions.
- **`prune` is a dry run by default** too, and keeps at least `--keep` newest
  snapshots whatever the age filter says.
- **Secrets are excluded** unless you say `--include-secrets`, which requires
  encryption and can never be combined with `--plaintext`. Environment secrets
  are never included at all.

Encrypt them. `--passphrase-env` names the variable holding the passphrase
(`GENUS_SNAPSHOT_PASSPHRASE` by default), and `--plaintext` is the explicit
opt-out that exists so nobody produces an unencrypted snapshot by forgetting
something.

`--skip-database` and `--skip-workspace` give you the two halves separately,
and `restore` has `--database-only` and `--workspace-only` to match — which is
how a workspace moves from a compose pilot to a Helm deployment without
carrying the pilot's database with it.

Put snapshots somewhere that is not the machine they came from, and put a
`verify` in whatever runs the schedule. The mechanics, including the retention
policy, are in [Snapshot and restore](../runbooks/SNAPSHOT_RESTORE.md).

## The migration ledger

The schema is a canonical ordered chain with a ledger, not a directory glob.

```bash
genus migrate --status
genus migrate --check
genus migrate --dry-run
genus migrate
```

`--status` prints the ledger: what has been applied, what is pending, and
whether anything has **drifted** — a row whose recorded checksum no longer
matches the migration this build ships. Drift is the condition that makes
`genus doctor --fix` refuse on purpose, and the right response is to read the
ledger, not to force anything.

Two adoption flags exist for a database this runner never wrote — one created
by an older installer, or migrated by a retired glob — and they record history
as applied without executing it. Use them once, knowingly, having read
[the schema section](../deployment.md#the-schema). They are not a repair.

## Upgrading

Whatever the substrate: **snapshot first, verify the snapshot, then upgrade.**
Read the [release notes](../release-notes.md) for your audience before you
start; that is the page that says what changed for the person doing this.

### A compose pilot

An image tag, and nothing else.

```bash
# 1. Name the release you are moving to -- a real tag. There is no `latest`.
# 2. Pull it before anything stops, so a bad tag fails while the old stack is up.
# 3. Reconcile: compose re-runs the one-shot `migrate` service and holds the
#    engine, bridge and orchestrator until it exits 0.
# 4. Ask, rather than assume:
genus doctor
```

The order matters. Pulling before stopping means a tag that does not exist
fails while the instance is still serving. The full command sequence is in
[Deployment](../deployment.md#upgrading).

### A Helm release

```bash
helm upgrade genus oci://ghcr.io/ironsail-llc/charts/genus-os \
  --version X.Y.Z \
  --namespace genus \
  --values values.yaml

helm test genus --namespace genus
```

Same values file, new chart version. The migration job runs as part of the
release; the engine's `/ready` will not pass until the workspace holds a
parsing `main` manifest, so a rollout that stalls unready after a workspace
change is telling you something true.

### A checkout

```bash
genus upgrade --dry-run
genus upgrade --pull
genus doctor
```

`--pull` is for a git checkout; a wheel install upgrades with pip and then
`genus migrate`. `--dry-run` prints what would change.

## What a release changes

Two documents, answering two questions:

- **[Release notes](../release-notes.md)** — what changed for operators, for
  admins, and for agent authors, in words written for each. This is the one to
  read before an upgrade.
- **`CHANGELOG.md`** in the repository — one line per conventional commit. The
  developer record, useful when you need to know exactly which commit did
  something.

Versions are semantic and the tag, the images and the chart version always
agree: the publish job refuses to push a chart whose version disagrees with the
release tag. There is no `latest` image tag, deliberately, so nothing upgrades
because a pull happened at the wrong moment.

## Rollback

1. Deploy the previous tag — the same upgrade command with the old version.
2. If the schema moved, restore the snapshot you took before the upgrade.
   `genus snapshot restore <file> --confirm --force` after a `verify`.
3. `genus doctor`, and do not declare it done before it exits 0.

Migrations are forward-only. That is why the snapshot is step zero of every
upgrade rather than a thing you consider afterwards: rolling the images back
does not roll the schema back, and a newer schema under older code is the
worst of the three possible states.

Rehearse this. A restore drill on a machine that is not production, on a
schedule, is the only way to know the number you would quote when somebody asks
how long a recovery takes.

Next: [Operate](07-operate.md).
