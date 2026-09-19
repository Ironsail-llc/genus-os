# 5. Secrets

Goal: know where this instance keeps each credential, which store wins, who can
read it, and what a shell command an agent runs gets to see.

Everything in the first half of this page is what the platform does **today**.
One clearly marked section at the end describes the change that is landing next,
and nothing outside that section describes behaviour you do not have.

## Where a credential comes from

One chain, for every credential, in this order:

1. **The process environment.** On a systemd install that is
   `/run/robothor/secrets.env`, loaded by `EnvironmentFile=` and written at boot
   by whichever backend `scripts/load-secrets.sh` dispatched to — the unit
   environment, a 0600 file, or SOPS + age. In a container it is simply the
   container environment. SOPS is how the file got there, not a place the
   platform looks.
2. **The AES vault** (`genus vault`), where the first-run wizard and the
   Settings page write credentials.
3. **Nothing** — reported as `missing` rather than guessed at.

**The environment wins.** A value exported into the unit environment shadows the
same name in the vault, silently, and that is the failure mode that costs an
afternoon: half the box uses one value and half the other, and every individual
check passes. If you are moving a credential into the vault, remove the
environment copy in the same change.

**A vault that cannot be read is `unavailable`, never `missing`.** A read
failure returns nothing rather than raising, and the source keeps the two apart:
`missing` means the vault answered and holds no row, `unavailable` means nobody
knows. That distinction is load-bearing — a vault whose read fails while its
write works would otherwise overwrite the live signing key on the next boot,
invalidating every session and every stored second factor.

## Working with the vault

```bash
genus vault init
genus vault list
genus vault set providers/openrouter/api_key
genus vault get providers/openrouter/api_key
genus vault delete providers/openrouter/api_key
genus vault audit
```

`genus vault set` prompts for the value when you leave it off the command line,
which is the only form worth typing. `genus vault import-env <file>` loads a
`.env` in one go.

## What an agent can see

Two containment questions an enterprise reader should ask, answered as the code
answers them today rather than as anyone would like.

**Can an agent read a stored credential? Yes.** The `vault_get` tool returns the
plaintext value to the model, and `vault_get` and `vault_list` sit in the
ordinary read-only tool set — an agent with a normal `tools_allowed` can call
them. If an agent must not read the vault, name `vault_get` and `vault_list` in
its manifest's `tools_denied`, and read the manifest to check what you actually
granted rather than assuming a default is narrow.

**What does a shell command an agent runs get to see? Everything the engine
has.** `exec` runs the command as a child of the engine process with the
engine's own environment, so every credential in that environment — the database
password, the provider keys, the channel tokens — is visible to it. There is no
per-agent environment allowlist today.

The boundary that does hold is the **sandbox**: with a non-local `sandbox:` mode
the command runs inside the container instead, which has its own environment and
not the engine's. For an agent whose instructions you do not fully control, that
is the containment to use, and it is the honest answer to "what stops a model
composing `env`".

## Handing the assistant a credential over a channel

You can ask an agent with the vault tools to store a credential for you, and it
will: `vault_set` writes it to the encrypted vault, and the change is live for
readers that consult the vault. Two things to know before you do it:

- **The agent can read the value back**, per the section above.
- **The value you pasted is in the conversation.** The platform's redactor takes
  credential-shaped text out of **log records and command lines** — the two
  places a credential used to escape into the journal — but it does not rewrite
  message history or a recorded run step. Treat a credential pasted into a chat
  as a credential that has been written down, and rotate it if that is not
  acceptable.

Both of those change with the release described at the end of this page. Until
then, for anything you would not want an agent to be able to echo, use
`genus vault set` on the box.

## Rotation

Rotation is a vault write, and readers pick it up without a redeploy —
provided no environment copy of that name is shadowing it. If one is, rotating
the vault row changes nothing, which is the shadowing problem at its most
confusing.

A provider credential has a second half the vault cannot reach: the engine's
credential pool, in memory, which retires a key the provider refused and holds
it out for a cooldown — six hours for a calendar quota. Topping up the account
or raising the limit does not tell it anything. **After a top-up, or any change
made at the provider, run `genus secrets reload`**: it mints a short-lived
`engine:control` token, makes the running engine re-read the vault, and puts
retired credentials straight back in rotation, with no restart and no
in-flight work cancelled. It prints the fingerprint of every credential that
came back; `genus secrets status` shows the same state per credential at any
time. The failure it exists for is in the [local fallback
runbook](../runbooks/LOCAL_FALLBACK.md).

One rotation to plan rather than perform casually: `GENUS_AUTH_SIGNING_KEY`
derives the key that encrypts stored second-factor secrets, so rotating it
invalidates every enrolled authenticator. See [Identity](02-identity.md).

## The rules the platform does enforce

- A credential is never a command-line argument. `genus channel add` refuses a
  token passed as a flag, because a command line is world-readable while the
  process lives, and argparse's own "unrecognized arguments" error is redacted
  so a mistyped flag cannot publish the token either.
- `genus config set` refuses a secret and names the vault command instead.
  Writing a credential into a config file is not a shortcut worth having.
- `genus config get` and `list` print `<set, sha256:ab12cd34>` for a secret —
  enough to tell two boxes apart without putting the value on your screen.
- There is no API that writes a channel credential. By design.
- An agent bundle that carries a credential literal fails to export. See
  [Agents](04-agents.md).

```bash
genus doctor --category secrets
```

Three checks: the secrets backend resolves, the auth signing key is where the
processes that need it read from, and the bridge's SSO secret is too. The
classic failure is that last one — a sign-in secret present in the workspace
file and absent from the unit environment. The bridge reports ready and nobody
can sign in.

## Where they live, per substrate

| Substrate | The environment | The vault |
|---|---|---|
| Compose | `genus.env` beside the compose files, mode 0600, read via `env_file` | In the mounted workspace |
| systemd | A root-owned SOPS file decrypted at boot into a tmpfs path the units read | In the workspace |
| Helm | The chart's secret classes — database, cache, signing, SSO, dashboard, provider — separately rotatable, with per-component references enforced | In the workspace volume, or use your cluster's own secret manager |

---

## Landing in the next release

!!! warning "None of this section is true of the release you are running"

    Everything below describes work on the `feat/vault-managed-secrets` branch,
    which has not merged. It is here so you can plan against it, and because two
    of the limitations described above are exactly what it removes. Nothing in
    this section is a control you have today. The commands in it do not exist
    yet, which is why they are exempt from the gate that executes every other
    command in this guide.

**The vault becomes the store you manage; the environment becomes bootstrap
only.** Credentials get classified into two sets that resolve in opposite
directions: *application* credentials (provider keys, source-control tokens,
channel tokens, SMTP passwords, webhook URLs) read vault-first, so a credential
you hand the assistant today beats what the box booted with in March; *bootstrap*
credentials (the database and cache passwords, the signing and SSO secrets, the
vault's own configuration) keep reading environment-first, so a process can
start before the vault is reachable. A name nobody has classified is an
application credential, which is the safe default. SOPS stays, holding only the
bootstrap set.

**The assistant will keep and prove a credential without being able to read
it.** `vault_set` will answer with a fingerprint (`sha256:1a2b3c4d`) rather than
an acknowledgement; a new `vault_test` will dial the vendor's own identity
endpoint and report an identity hint or an error class, so "configured" and
"working" stop being the same claim; and `vault_get` will return the key,
whether it is configured, its fingerprint, its source and when it changed —
never the value. The pasted value will be scrubbed out of the transcript and out
of the recorded step.

**The vault tools will be gated on the calling agent's own manifest** — a
credential tier that is deliberately not the `role:` field, which feeds RBAC and
would otherwise deny the agent every tool. A spawned sub-agent runs under its
own agent id, so its own manifest is what is checked: a parent holding the tier
will not lend it.

**`exec` will get an environment allowlist.** A shell child will see the process
essentials and the non-secret settings, and a credential only when that agent's
own manifest names it, under a governed ladder so an existing install can watch
the change before enforcing it. Bootstrap names will be refused from such a
grant however they are spelled. The sandbox stays the boundary for an agent you
have decided not to trust.

**Two new verbs and a doctor check** will move credentials out of the
environment and then prove nothing is configured twice, with different values,
in the two stores:

<!-- doc-check: skip -->
```bash
genus secrets status
genus secrets migrate --from-env --dry-run
genus secrets migrate --from-env
genus doctor --only secrets.shadowed
```

`migrate` will refuse bootstrap names, skip values that already match, and
report a differing vault row as a conflict rather than overwriting it. It will
never print a value, not even under `--dry-run`.

Next: [Backup and upgrade](06-backup-upgrade.md).

## Personal information for delegated tasks

Open **Account → Personal automation** to enroll profiles, logins, documents,
authenticator keys and personal payment resources, then grant bounded standing
authority. These resources extend the native vault and return references to
agents. See [Personal autonomous execution](../AUTONOMOUS_EXECUTION.md) for
setup, key rotation, verification and the separate payment deployment gate.
