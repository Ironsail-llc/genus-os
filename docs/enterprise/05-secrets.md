# 5. Secrets

Goal: one managed store your assistants can write to, an environment that
holds only what has to exist before the process starts, and a credential you
can rotate without editing a file on a box.

!!! note "Some of this lands in the next release"

    The vault, `genus vault`, the agent credential tier and the doctor's
    secrets checks are shipped today. The `genus secrets` verbs, the
    application-versus-bootstrap classification, and the `secrets.shadowed`
    doctor check are **landing in the next release** — this page describes the
    shape they land in, and each such command is marked. Until then, read
    [Configuration](../configuration.md#secrets) for the store as it is now.

## The rule

**The vault is the store you and the assistant manage. The environment — which
on a systemd instance is a root-owned SOPS file decrypted at boot — is
bootstrap only.**

Bootstrap is the small set of values a process needs in order to start at all:
the database password, the cache password, the signing and SSO secrets, the
vault's own configuration. Everything else is an **application** credential: a
provider key, a source-control token, a channel token, an SMTP password, a
webhook URL. A name nobody has classified is an application credential, which
is the safe default.

The two classes resolve in opposite directions, and that is the whole design:

| Class | Read order | Why |
|---|---|---|
| Application | vault, then environment | So a credential you hand the assistant today beats what the box booted with in March |
| Bootstrap | environment, then vault | So a process can start before the vault is reachable |

An unreadable vault still falls through to the environment. The store being
down is not the same event as the credential being absent, and neither is the
same as it being wrong.

SOPS does not go away. It stays as the bootstrap layer; what changes is how
little it has to hold.

## Handing the assistant a credential

You can give an agent a new credential in a conversation, on whichever channel
you already use, without opening a shell. Three steps, and the agent runs all
three:

1. **Store.** The agent writes it to the vault. What it gets back is the key
   and a **fingerprint** (`sha256:1a2b3c4d`) — never the value.
2. **Prove.** The agent dials the vendor's own identity endpoint with the
   credential and reports what came back: an identity hint, or an error class.
   "Configured" and "working" are different claims, and only one of them is
   worth telling you.
3. **Answer.** The agent replies with the fingerprint and the test result.

It will not echo the value back, and it **cannot read it**: the read tool
returns the key, whether it is configured, its fingerprint, its source and when
it changed, and never a value. The pasted value is scrubbed out of the
transcript and out of the recorded step, so it does not survive in run history.

The fingerprint is what makes this operationally useful. Two boxes that should
have the same key either show the same eight characters or they do not, and you
never had to look at the key to find out.

## What a sub-agent can see

Two separate grants, and neither is inherited.

**The vault tools** are gated on a key in the agent's own manifest — a
credential tier, deliberately *not* the `role:` field, which feeds RBAC and
would otherwise deny the agent every tool. A spawned sub-agent runs under its
own agent id, so its own manifest is what is checked. A parent holding the tier
does not lend it.

**Shell credentials** are separate again. An agent's `exec` children get the
process essentials and the non-secret settings, and nothing else, unless that
agent's own manifest lists the credential by name. Bootstrap names are refused
from that list however they are spelled.

The scrub is a boundary against accident and against a model composing a
command, not against an agent you have decided not to trust. For that, the
boundary is the sandbox: a containerised agent gets no host environment at all.

## Moving out of the environment

*Landing in the next release.*

<!-- doc-check: skip -->
```bash
genus secrets status                      # what is where, and which store wins
genus secrets migrate --from-env --dry-run
genus secrets migrate --from-env
```

`migrate` refuses bootstrap names, skips values that already match, and reports
a vault row that differs as a conflict rather than overwriting it — you name
such a row explicitly to replace it. It never prints a value, not even under
`--dry-run`. Then shrink the SOPS file to the bootstrap set, restart the units
once to prove the box still starts, and check nothing is configured two ways:

<!-- doc-check: skip -->
```bash
genus doctor --only secrets.shadowed
```

That check fails when an application credential is set differently in the two
stores, naming each one with both fingerprints. A value configured twice, with
two different values, is the failure mode that costs a whole afternoon: half
your processes use one and half the other, and every individual check passes.

Today, without those verbs, the same operations are:

```bash
genus vault list
genus vault set providers/openrouter/api_key
genus vault delete providers/openrouter/api_key
genus vault audit
```

`genus vault set` prompts for the value when you leave it off the command line,
which is the only form worth typing.

## Rotation

Rotation is a vault write, and it is live: the readers reload, so nothing needs
restarting and nothing needs redeploying. That is the point of keeping
application credentials out of the environment — an environment credential
cannot be rotated without restarting whatever read it.

A rotation you should plan rather than perform casually:
`GENUS_AUTH_SIGNING_KEY` derives the key that encrypts stored second-factor
secrets, so rotating it invalidates every enrolled authenticator. See
[Identity](02-identity.md).

## The rules the platform enforces

- A credential is never a command-line argument. `genus channel add` refuses a
  token passed as a flag, because a command line is world-readable while the
  process lives.
- `genus config set` refuses a secret and names the vault command instead.
  Writing a credential into a config file is not a shortcut worth having.
- `genus config get` and `list` print `<set, sha256:ab12cd34>` for a secret —
  enough to tell two boxes apart without putting the value on your screen.
- There is no API that writes a channel credential. By design.
- Every log line, every audit row, every error field and every channel health
  report passes through the platform's redactor before it leaves the process.
- An agent bundle that carries a credential literal fails to export. See
  [Agents](04-agents.md).

## Where they live, per substrate

| Substrate | Bootstrap | Application |
|---|---|---|
| Compose | `genus.env` beside the compose files, mode 0600, read via `env_file` | The vault, in the mounted workspace |
| systemd | A root-owned SOPS file decrypted at boot into a tmpfs path the units read | The vault, in the workspace |
| Helm | The chart's secret classes — database, cache, signing, SSO, dashboard, provider — separately rotatable, with per-component references enforced | The vault, or your cluster's secret manager |

`genus doctor --category secrets` is the check that they are where the
processes that need them actually read from. The classic failure is a sign-in
secret present in the workspace file and absent from the unit environment: the
Bridge reports ready and nobody can sign in.

Next: [Backup and upgrade](06-backup-upgrade.md).
