# Shrinking the SOPS file to bootstrap

**Who this is for:** an operator whose `secrets.enc.json` has grown to hold
every credential the instance has ever used, and who wants the assistant to be
able to rotate most of them.

## Why

On 2026-09-15 the operator handed the assistant a GitHub token over Telegram and
expected it to be kept, used and rotated by the assistant. It could not be. The
assistant can write the vault, but the secrets accessor read the process
environment first — and that environment is a snapshot of this SOPS file,
decrypted at boot into `/run/robothor/secrets.env` by a root-owned
`ExecStartPre`. So the expired `GH_TOKEN` in the file shadowed the fresh vault
row, and clearing it needed root to edit the file and restart the unit. The
assistant can do neither, and must never need to.

Application credentials now resolve **vault-first**, so the shadow no longer
breaks anything. But a stale copy in this file is still a credential somebody
will eventually read and a rotation somebody will think they performed — which
is why `genus doctor` reports it. The fix is to stop keeping it here.

## What belongs in the file

Only **bootstrap** credentials: the ones that bring the instance up, which by
definition cannot come from a store the instance needs to be up to read.

| Name | Why it must be here |
|------|---------------------|
| `ROBOTHOR_DB_PASSWORD` | The vault's rows live in this database. A vault-first lookup for it asks the vault for the key to the vault. |
| `ROBOTHOR_REDIS_PASSWORD` | Read before the engine has a database connection. |
| `GENUS_AUTH_SIGNING_KEY` | Rotating it signs every session out and makes every stored MFA secret undecryptable. |
| `AUTH_SECRET` | The dashboard's own session key, read by the Next.js app. |
| `GENUS_BRIDGE_SSO_SECRET` | The bridge refuses every sign-in without it; see the 2026-09-03 eight-day outage. |
| `ROBOTHOR_INTENT_HMAC_SECRET` | Changing it under running verifiers invalidates in-flight intents. |
| `ROBOTHOR_NATS_URL`, `ROBOTHOR_NATS_PASSWORD` | Substrate transport, dialled before subsystems start. |
| `ROBOTHOR_TEST_DB_DSN`, `ROBOTHOR_TEST_ADMIN_DSN` | Same argument as the database password. |

`SOPS_AGE_KEY_FILE` and `ROBOTHOR_VAULT_*` are bootstrap by construction — they
are how the file and the vault are opened — and are not stored *in* the file.

The authoritative list is not this table. It is the `bootstrap=True` marker on
the settings declaration, readable as:

```bash
genus secrets status          # the `bootstrap` note on each row
```

Everything else — provider keys, `GITHUB_TOKEN`, channel tokens, SMTP
passwords, webhook URLs — is an **application** credential and belongs in the
vault.

## The move

**The order matters, and it is: migrate → verify → shrink.** Shrinking the SOPS
file before verifying is what turns a mis-filed credential into an outage, and
there is no step here that can be usefully done out of order.

```bash
# 1. See what is where.
genus secrets status

# 2. Rehearse. Prints exactly what step 3 will print, including conflicts.
genus secrets migrate --from-env --dry-run

# 3. Migrate. Names and fingerprints only; never a value.
genus secrets migrate --from-env

# 4. VERIFY before touching the SOPS file. Every credential you migrated should
#    read `vault` under SERVED. Anything that does not is a credential the
#    readers cannot see, and deleting its environment copy would take it away.
genus secrets status
genus doctor --only secrets.shadowed

# 5. Only now, shrink the file (below).
```

`migrate` opens by telling you how many existing vault rows it will **not**
touch, before any per-name line — because on a box whose assistant has been
rotating credentials, that number is the one that matters.

### What the verify step can and cannot tell you

`genus secrets status` and `secrets.shadowed` measure the **accessor**
(`robothor.secrets.resolve_secret`). They say `served=vault` when the accessor
would serve the vault's copy — which is the right answer for every reader that
goes through the accessor, and says nothing at all about one that does not.

Every credential the platform reads now goes through the accessor, and a guard
test (`test_readers_use_the_accessor.py`) fails the build if a declared,
migratable credential is read straight from the environment again. That guard is
what makes this verify step honest; it exists because three readers — the
Telegram bot token, the alert webhook and the SIEM webhook — did not, so the
table reported them safe to remove and the next restart would have taken
Telegram down on an operator whose only channel is Telegram.

If you are running an older engine than this branch, do **not** shrink
`ROBOTHOR_TELEGRAM_BOT_TOKEN`, `ROBOTHOR_ALERT_WEBHOOK_URL` or
`ROBOTHOR_SIEM_WEBHOOK_URL` whatever the table says.

`migrate` refuses bootstrap names outright, so step 3 cannot move something the
box needs in order to start.

**It also never overwrites a vault row that differs.** If the assistant has
already rotated a credential, the vault holds the new one and the environment
holds the dead one the box booted with — so a migration that wrote would revert
the rotation, which is the incident performed by its own remedy. Those names are
reported as `CONFLICT` with a fingerprint per store and skipped. Check which copy
is alive (`vault_test`) before doing anything; if the environment's really is the
one you want, `--overwrite NAME` replaces that one row and no other.

### Then delete the migrated entries from the file

```bash
sops /etc/robothor/secrets.enc.json
```

Remove every name `migrate` reported as stored — and every name it reported as
`CONFLICT`, once you have confirmed the vault's copy is the one you want, since
those are exactly the environment entries that were shadowing a rotation.
Compare fingerprints first if you want to be certain:

```bash
genus secrets status | grep GITHUB_TOKEN
```

A row with `yes` under both ENV and VAULT and a `SHADOW` note means the two hold
**different** values — decide which one you meant before deleting either.

### Verify

```bash
genus doctor --only secrets.shadowed
sudo systemctl restart robothor-engine    # only to prove the box still starts
genus secrets status
```

`secrets.shadowed` should pass, and every migrated credential should read
`vault` under SERVED with nothing under ENV.

## Rolling back

Nothing is destroyed by the migration: it copies. If a credential turns out to
be needed at boot, put it back in the SOPS file — bootstrap precedence means
the environment will win for it again as soon as you also mark it bootstrap in
the settings model, and until then the vault row keeps working.

## What did not change

SOPS stays. It is still the backend `scripts/load-secrets.sh` dispatches to, it
still decrypts to tmpfs at boot, and `genus init --secrets-backend sops` still
sets it up. The only change is what it is asked to hold.

One related adjustment: `REQUIRED_KEYS` in `scripts/decrypt-secrets.sh` is now
empty. It listed `OPENROUTER_API_KEY`, which would have refused the boot of an
instance whose provider key lives in the vault — and since that script is the
`ExecStartPre` of a unit ordering four services, a refusal there is the whole
instance in `dependency failed`. The key is still *warned* about by name, with a
pointer to `genus secrets status`, which is the command that can see both
stores.
