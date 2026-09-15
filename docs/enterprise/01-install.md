# 1. Install the pilot

Goal: a running instance, `genus doctor` exit 0, and an owner account you
signed in with. Budget half an hour on a machine that already has Docker.

Everything on this page is the short form of [Quick Start](../quickstart.md),
which carries the prerequisites, both substrates in full, and the nightly CI
job that replays its commands on a clean machine.

## The one-liner

```bash
curl -fsSL https://ironsail-llc.github.io/genus-os/install.sh | bash -s -- --substrate compose
```

That deliberately installs nothing. A pipe has no terminal on the other end, so
there is nowhere to confirm a plan; the script prints what it would do and
stops. Read it, then run it for real:

```bash
curl -fsSL https://ironsail-llc.github.io/genus-os/install.sh | bash -s -- \
  --substrate compose --yes \
  --owner-name "Ada Lovelace" --owner-email ada@example.com
```

Rather not pipe a URL into a shell — and in a company you probably should not —
download it, read it, run it:

```bash
curl -fsSLO https://ironsail-llc.github.io/genus-os/install.sh
less install.sh
bash install.sh --substrate compose --yes \
  --owner-name "Ada Lovelace" --owner-email ada@example.com
```

Export a provider key first (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `GROQ_API_KEY`); the script never
prompts for one, and the browser wizard will ask if you skip it.

What the script will not do: run as root, run under `sh`, `sudo` anything, or
download from `main` rather than a release tag.

## The compose pilot, by hand

The same thing, step by step, when you want to see each part. The wheel does
not carry the compose files, so fetch the two the stack is made of:

```bash
pip install genusos
mkdir -p ~/genus && cd ~/genus
GENUS_TAG="v$(genus --version | awk '{print $NF}')"
curl -fsSLO "https://raw.githubusercontent.com/Ironsail-llc/genus-os/$GENUS_TAG/infra/docker-compose.yml"
curl -fsSLO "https://raw.githubusercontent.com/Ironsail-llc/genus-os/$GENUS_TAG/infra/docker-compose.apps.yml"
export ROBOTHOR_DB_PASSWORD=choose-a-password
export OPENROUTER_API_KEY=sk-your-key
genus init --substrate compose --yes --workspace . --owner-name "Ada Lovelace" --owner-email ada@example.com
export ROBOTHOR_WORKSPACE="$PWD"
genus doctor --json
```

Fetch the compose files at the tag of the CLI you installed, never at `main`:
the substrate writes that version into `genus.env` as the image tag, so compose
files from `main` would describe a stack the pinned images do not match. There
is no `latest` tag to pull by accident.

There is also no migration step to run. The compose file carries a one-shot
`migrate` service that the engine, bridge and orchestrator wait on with
`service_completed_successfully`.

Run it as the account that will own the instance, not as root. `genus init`
refuses a root install rather than leaving files nothing else can open.

## Helm, for a cluster

Every release publishes the packaged chart to GHCR, so an install needs no
clone:

```bash
helm show values oci://ghcr.io/ironsail-llc/charts/genus-os --version X.Y.Z > values.yaml

helm install genus oci://ghcr.io/ironsail-llc/charts/genus-os \
  --version X.Y.Z \
  --namespace genus --create-namespace \
  --values values.yaml

helm test genus --namespace genus
```

The chart version is the release tag with the leading `v` removed; the publish
job refuses to push when the chart and the tag disagree.

The values you must decide, rather than accept:

| Value | Why you must choose |
|---|---|
| `postgres.enabled`, `redis.enabled` | The chart brings subcharts so a bare cluster renders. In production you almost certainly want your own managed PostgreSQL 16 with pgvector, and your own Redis |
| The secret classes | Database, cache, signing, SSO/OIDC, dashboard and provider material are separately rotatable, with per-component references enforced. The dashboard and the migration job cannot request the privileged or provider classes — keep it that way |
| `workspace.persistence` or `workspace.configMap` | The engine's `/ready` will not pass until the workspace holds a parsing `main` agent manifest. Either mount a PVC and seed it (a provisioning job running `genus init`, or a restored snapshot), or mount an immutable workspace from a ConfigMap |
| `workspace.allowEmptyFleet`, `minAgentCount`, `requiredAgentIds` | Production sets `false`, `1`, `[main]`. A fresh PVC starts unready on purpose: an engine with no fleet is not something to route traffic at |

An upgrade is the same line with `helm upgrade`. Details, including the
from-a-checkout path for chart development, are in
[Deployment](../deployment.md#helm).

## The acceptance gate

`genus doctor` is the single answer to "is this instance actually working?".
Do not go to page 2 until it exits 0.

```bash
genus doctor
genus doctor --json
genus doctor --only db.migrations
genus doctor --category secrets
genus doctor --fix --dry-run
genus doctor --fix
```

`✓` is a pass, `✗` a failure, `·` a check that could not run — **a skip is
never a pass**. Severity decides what is fatal: `required` means the instance
does not work, `recommended` means something is drifting, `info` is for the
record.

| Exit code | Meaning |
|---|---|
| 0 | No `required` check failed |
| 1 | A `required` check failed |
| 2 | The doctor itself could not run — an unknown `--only` id, a registry that would not import |

2 is deliberately separate from 1: "nothing is wrong" and "nothing was checked"
must never share an exit code, or a typo in a CI gate becomes a permanently
green build.

`--fix` covers exactly three repairs — pending migrations, the `service` RBAC
role, and the tenant-isolation policy on a table that lacks it — and each
re-runs its own check afterwards. `identity.owner_account` is never
auto-fixed: minting a privileged account from a diagnostic that can run on a
timer would be an escalation path, not a repair. The three findings a fresh
install actually hits, and what to do about each, are in
[Quick Start](../quickstart.md#when-the-doctor-is-red).

## The setup wizard

`genus init` ends by printing a `/setup` link that works once, for thirty
minutes. That page is the only way into a fresh instance: the install
deliberately creates no account, because an install that claimed the instance
on your behalf would close `/setup` before you ever opened it.

Six steps: welcome, operator account, provider key (tested with a real
completion before it lets you past), an optional channel token, an agent preset
(`minimal`, `standard` or `full`), and enrolling a second factor. From the
provider step onward the page carries a strip of the doctor's required checks,
so you watch the instance become healthy as you fill it in.

Three properties worth knowing before you hand the link to somebody:

- Only the token's SHA-256 digest is stored, and the token never persists in
  the browser — not `localStorage`, not a cookie — and is stripped from the
  address bar as soon as it is spent.
- The moment an operator account exists, `/setup` and every route behind it
  answer 404 for good, and no further link can be minted. The completion signal
  is the owner row in the database, not a file.
- Lost it, or installed headless? `genus auth setup-link` on the server mints a
  fresh one. It is local-only on purpose: shell access is the one credential
  the gate can rely on before an account exists. `127.0.0.1` is loopback on the
  *server*, so `genus init` prints an `ssh -L` line for that case.

The first-run URL is also the first key in `genus init --yes --json`'s output,
if you are scripting this.

Next: [Identity](02-identity.md) — getting the rest of your people in.
