# Writing a Genus plugin

Genus discovers extensions through Python entry points. There is no registry
to publish to and no package format to learn: if `pip` can install it, Genus
can load it.

## A tool plugin

```toml
# pyproject.toml
[project]
name = "genus-acme-tools"
version = "0.1.0"

[project.entry-points."genus.tools"]
acme = "acme_tools:PLUGIN"
```

```python
# acme_tools/__init__.py
async def coin_flip(args, ctx=None):
    """Handlers take (args, ctx) — the same signature the engine's own use."""
    return {"result": "heads"}

PLUGIN = {
    "genus_contract_version": "1.0",
    "handlers": {"coin_flip": coin_flip},
}
```

`pip install genus-acme-tools`, restart the engine, and `coin_flip` is
available to any agent whose manifest lists it in `tools_allowed`.

## Groups

| Entry-point group | Payload key | Contributes |
|---|---|---|
| `genus.tools` | `handlers` | tool implementations |
| `genus.schemas` | `schemas` | the OpenAI-style function schema a tool needs |
| `genus.guardrails` | `policies` | pre/post execution policies |
| `genus.hooks` | `hooks` | lifecycle hooks |
| `genus.models` | `models` | token limits and pricing for models the built-in table does not know |
| `genus.jobs` | `jobs` | work that runs on a schedule, not on a tool call |
| `genus.services` | `services` | **any** named service — the group that names no kind |
| `genus.commands` | `commands` | operator verbs — `robothor <verb>` |
| `genus.sandboxes` | `sandboxes` | alternative sandbox runtimes (**opt-in**, see below) |
| `genus.channels` | `channels` | outbound/inbound channel implementations (**opt-in**, see below) |
| `genus.memory` | `providers` | extra memory sources, merged **after** built-in recall |
| `genus.doctor` | `checks` | `genus doctor` checks — ids must be prefixed with the distribution's own name and may not shadow a built-in |

A tool normally ships in two groups: the handler and its schema. The engine
keeps those in separate registries and so does the loader.

## The rules, and why

**The contract version is checked and mismatches are refused.** `1.0` today.
A plugin built against a different contract is not loaded — an agent
platform running third-party code that expects a different tool-calling
contract is a security problem, not a compatibility inconvenience.

**A plugin cannot shadow a built-in.** Claiming `exec` or `write_file` is
refused and logged. That is a takeover, not an extension.

**Two plugins cannot claim the same name.** The second is refused rather
than silently overriding the first.

**Refusal is per-plugin and total.** A plugin whose names partly collide
loads none of them: half-loading leaves it in a state its author never
tested. And a plugin that fails to import is recorded and skipped — one
broken package must never stop the engine booting.

## Checking what loaded

Plugin failures are logged at WARNING with the plugin name and the reason:

```
Plugin 'acme' declares contract '0.9', this engine speaks '1.0' — refused
Plugin 'acme' tried to shadow built-in(s) ['exec']
```

## A model plugin

The built-in table cannot know every model an instance runs. A plugin adds
coverage without editing the engine:

```python
# acme_models/__init__.py
from robothor.engine.model_registry import ModelLimits

PLUGIN = {
    "genus_contract_version": "1.0",
    "models": {
        "openrouter/acme/big-v1": ModelLimits(
            max_input_tokens=400_000,
            max_output_tokens=8_192,
            default_output_tokens=4_096,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
        ),
    },
}
```

A plain dict with the same keys works too, so a plugin need not import the
dataclass. Entries are consulted **after** the curated registry and
**before** litellm's bundled catalog: a plugin extends what the engine knows
about, and can never overwrite a model the platform pinned deliberately.

## A scheduled job

Everything the engine runs on a schedule is registered from inside the
package. A plugin can add its own:

```python
# acme_jobs/__init__.py
async def nightly_sweep():
    ...

PLUGIN = {
    "genus_contract_version": "1.0",
    "jobs": {
        "nightly_sweep": {"cron": "30 2 * * *", "func": nightly_sweep},
    },
}
```

Jobs are registered as `plugin:<name>`. That prefix is deliberate: the
scheduler's reconcile rebuilds the live job set from what the agent
manifests declare and removes everything else, and a manifest cannot know
about a plugin's job. The `plugin:` namespace is exempt, so a contributed
job is never culled — an earlier engine job that lacked such a namespace ran
for at most five minutes per engine lifetime before anyone noticed.

An entry needs a five-field cron expression and a callable. Anything
malformed is skipped with a warning rather than raised.

## An operator command

```python
# acme_ops/__init__.py
def run_drill(args):
    ...
    return 0

PLUGIN = {
    "genus_contract_version": "1.0",
    "commands": {
        "restore-drill": {"help": "Run a restore drill", "func": run_drill},
    },
}
```

`robothor restore-drill` then works and appears in `--help`. The handler
receives the parsed `argparse` namespace and returns an exit code.

Built-in verbs always win: the set is read off the parser itself and passed
to the loader as reserved, so a package cannot claim `migrate`, `snapshot`
or `serve`. The list is derived rather than hand-maintained, because a
second copy would drift the first time a subcommand was added.

## A sandbox runtime — the one group installation does not activate

Every group above takes effect as soon as the package is installed. This one
does not, and neither does a channel, below — both differences are
deliberate.

The sandbox is what confines untrusted execution. A package able to replace
it merely by being present could replace it with a no-op, and nothing would
look any different. So an installed backend stays inert until the operator
names it:

```bash
ROBOTHOR_SANDBOX_BACKEND=gvisor
```

```python
# acme_sandbox/__init__.py
def build_argv(*, workspace, run_id, cdp_port=None):
    return ["runsc", "run", "--rootfs", workspace, run_id]

PLUGIN = {
    "genus_contract_version": "1.0",
    "sandboxes": {"gvisor": {"build_argv": build_argv}},
}
```

The backend owns the whole argv — a backend that could only prepend flags
could not express a different isolation model, which is the point of having
one. Naming a backend that is not installed **raises**; it never falls back
to the built-in runtime, because a silent fall-back would turn a
misconfigured hardening step into an invisible downgrade.

## A channel — the other group installation does not activate

A channel is where the operator's output goes — Telegram, the event bus, or
whatever a plugin adds. A package able to become that surface merely by
being installed could intercept every briefing, and nothing would look any
different. So, like a sandbox backend, an installed channel stays inert
until the operator names it:

```bash
ROBOTHOR_CHANNELS=acme
```

```python
# acme_channel/__init__.py
from robothor.engine.channels.base import SendReceipt

class AcmeChannel:
    name = "acme"
    inbound_router = None

    async def start(self): ...
    async def stop(self): ...
    async def health(self):
        return {"ok": True}

    async def send(self, target, text, **kw) -> SendReceipt:
        # One entry per chunk the platform confirmed; an empty list means
        # nothing landed, and never raise for an ordinary failure.
        landed = await acme_sdk.post(target, text)
        return SendReceipt(
            acknowledged=len(landed),
            expected=1,
            platform_ids=[str(m.id) for m in landed],
        )

PLUGIN = {
    "genus_contract_version": "1.0",
    "channels": {"acme": AcmeChannel()},
}
```

A receipt is derived from what the sender returned, never from reaching the
next line: `SendReceipt(acknowledged=..., expected=..., platform_ids=[...])`.
A channel that acknowledges nothing is recorded `failed:`, the same rule
`delivery.py` applies to the built-in senders. Return one entry per chunk that
actually landed — an empty list when none did, and never an API response
object, which the platform refuses to read as proof.

**`send` is the only method the platform calls today.** `start`, `stop` and
`health` are part of the protocol because a channel that *receives* needs them,
and the shape should not change when the inbound half lands — but nothing drives
them yet, so open your transport lazily inside `send`. A channel that connects
in `start()` will pass its own tests and deliver nothing.

`telegram`, `event_bus`, `slack`, `webchat` and `email` are built in and
reserved — a plugin claiming one of those names is refused by the loader, the
same as claiming a built-in tool. `slack` is reserved even on an instance that
has never configured it: the name resolves to the built-in channel, which
answers `failed:slack_not_configured` rather than leaving the operator to guess
whether the platform has Slack support at all. See
[the Slack channel page](channels/slack.md) for setting it up. `webchat` is
reserved for a stronger reason — it writes into a member's own chat session and
inbox, so a plugin able to claim the name could redirect every member-facing
delivery while the session it wrote to still looked like the member's. See
[the web chat page](channels/webchat.md). `email` is reserved on that same
stronger footing: a plugin able to claim it would become the surface the
`crm_people.do_not_contact` guard runs inside, and an opt-out control a package
can replace merely by being installed is not a control. See
[the email channel page](channels/email.md).

Naming a channel that is not installed does not fall back to Telegram:
delivery records `failed:no_channel:<name>` instead.

**Declare your chunk size if you split.** `register_platform_sender(...,
chunk_size=N)` is how the platform tells a truncated send from a complete one.
Without it, two answers are recorded `failed:<name>_unproven`, because neither
can be read: more than one message back for the one body you were handed (you
split, so "landed all five" and "landed two of five" look identical), and one
message back for a body longer than 4,000 characters — the shortest limit any
surface here has, and the case where a sender that split into five and landed
*one* is indistinguishable from an honest single send.

## A named service — the group that names no kind

Every group above names a kind the platform already knows about. This one
does not, deliberately.

```python
# acme_vectors/__init__.py
class VectorStore:
    def search(self, q): ...

PLUGIN = {
    "genus_contract_version": "1.0",
    "services": {"vector_store": VectorStore()},
}
```

Anything can then reach it:

```python
from robothor.engine.services import get_service
store = get_service("vector_store")      # None if nothing provides it
```

and a tool handler gets it from its context: `ctx.get_service("vector_store")`.

Core owns a reserved set — `memory`, `scheduler`, `runner`, `session`, `llm`,
`sandbox`, `guardrails`, `tools`, `config`, `db`. Registering one is a
takeover, not an extension, and **the refusal is all-or-nothing**: a package
declaring one reserved name loses every service it registered. A package
reaching for `memory` has shown what it is willing to do.

## Reloading without a restart

`systemctl reload robothor-engine` (SIGHUP) re-reads installed plugins in
place. Tools, schemas, guardrails, hooks and models all pick up the change
on their next use; runs already in flight are not disturbed. A plugin that
has been uninstalled is withdrawn by the same reload.

## Declaring what you contribute

Ship a `genus-plugin.yaml` inside your package and list it as package data:

```yaml
# genus_acme/genus-plugin.yaml
name: genus-acme-tools
contract_version: 1
handlers:
  - coin_flip
```

```toml
[tool.setuptools.package-data]
genus_acme = ["genus-plugin.yaml"]
```

Genus reads this from the **packaging layer** — the installed distribution's
file list — *before* importing your module. Every other check (contract
version, reserved names, clashes) necessarily runs after `ep.load()`, which has
already executed your module body inside the daemon. The manifest is the only
one that can refuse a plugin without running it.

Controlled by `ROBOTHOR_PLUGIN_MANIFEST_MODE`:

| mode | behaviour |
|---|---|
| `off` | no manifest check |
| `observe` (default) | logs distributions that ship none, **still imports them** |
| `enforce` | refuses an unmanifested distribution before import |

`observe` is the default because requiring a manifest is a breaking change for
plugins published before it existed. **It does not protect** — it still imports,
it only says so. The pre-execution guarantee exists solely in `enforce`.

> **Editable installs (`pip install -e .`) cannot ship a manifest.** They expose
> only a `.pth` shim to the packaging layer, so the file is invisible to it and
> `enforce` will refuse the plugin. This is a real limitation of development
> mode, not something to work around by importing the package to find its own
> manifest — that would defeat the entire point. Use a normal install to test
> under `enforce`.

## Disabling and the lockfile

`pip install` used to be the whole of plugin governance: a distribution that
publishes a `genus.*` entry point became part of the engine, and the only way
to stop it was to uninstall the package. `plugins.lock` is the record that was
missing — what the operator accepted, what its manifest looked like at the
time, and which distributions are turned off.

It lives at `<workspace>/.robothor/plugins.lock` (override with
`ROBOTHOR_PLUGIN_LOCKFILE`), is written mode `0600`, and holds one JSON object
per **distribution** — not per entry point, because a distribution is what you
install, what gets recorded, and what `enable`/`disable` act on:

```json
{
  "lockfile_version": 1,
  "plugins": [
    {
      "name": "genus-hostinfo",
      "version": "0.1.0",
      "manifest_sha256": "…",
      "verdict": "unscanned",
      "enabled": true,
      "kinds": ["genus.schemas", "genus.services", "genus.tools"],
      "recorded_at": "2026-09-15T00:00:00+00:00"
    }
  ]
}
```

`verdict` is always `unscanned` today. Nothing scans a plugin yet, and a field
reading `safe` because no scanner ran would be worse than no field at all.

### The verbs

| command | what it does |
|---|---|
| `genus plugin list` | what is installed, what it contributes, what is disabled, what was refused |
| `genus plugin info <name>` | one distribution: manifest, groups, contributions, lock row, load state |
| `genus plugin sync` | upsert a row per installed distribution; drop rows for ones that are gone |
| `genus plugin enable <name>` | let a recorded plugin load again |
| `genus plugin disable <name>` | stop it being imported at all |
| `genus plugin doctor [--json]` | the `plugins` category of `genus doctor` |

`enable` and `disable` write the file and nothing else — the running engine is
still serving the set it discovered until you reload it (SIGHUP, or `POST
/api/plugins/reload`). An unknown name exits 2 rather than creating a row: a
disable that invented a row for a typo would report success and change nothing.

### What the loader does with it

Consulted **before `ep.load()`**, which is the only point at which refusing
means anything:

* a row with `"enabled": false` → refused, `disabled by operator`, never imported;
* a row whose `manifest_sha256` no longer matches the manifest on disk → refused,
  `manifest changed since it was recorded; run genus plugin sync`. This is the
  rule `verify_adapter_integrity` applies to a pinned stdio command, one layer
  up: a declaration that updates itself is self-approving, and "it used to be
  fine" is not a verification;
* a distribution with **no row** → loads exactly as it does today.

That last one is the important one. **The lockfile is opt-in**: it constrains
what it has been told about, so a fresh install behaves as it always has and
`genus plugin sync` is what turns the control on. A lockfile that does not
parse is treated as absent, logged once, and reported by the doctor — a
governance file that could brick an engine would be deleted by the first
operator it bricked.

### The doctor

| check | severity | what it asks |
|---|---|---|
| `plugins.lockfile` | recommended | present, parseable, mode 0600 |
| `plugins.load` | required | every installed plugin loaded, or is disabled on purpose |
| `plugins.drift` | required | no recorded manifest differs from what is on disk |

`plugins.load` accepts exactly one refusal — `disabled by operator`, which is
the operator's own decision arriving back at them. `plugins.drift` overlaps it
deliberately: both go red for a drifted plugin, and this is the one that names
`genus plugin sync`.

### Over HTTP

The engine owns all four answers, because a plugin is an object in *that*
process; the bridge proxies them behind `require_operator` and audits the three
acts with identifiers only.

| route (bridge) | engine | what it does |
|---|---|---|
| `GET /api/plugins` | `GET /api/admin/plugins` | generation, lockfile state, one row per distribution |
| `POST /api/plugins/{name}/enable` | `POST /api/admin/plugins/{name}/enable` | flip the row; 404 if unrecorded; does **not** reload |
| `POST /api/plugins/{name}/disable` | `POST /api/admin/plugins/{name}/disable` | as above |
| `POST /api/plugins/reload` | `POST /api/admin/plugins/reload` | runs the SIGHUP body; returns `{generation, loaded, failures}` |

No response carries a filesystem path. Which distributions are installed is a
platform fact; where an instance keeps its files is not, so the listing answers
`path_configured` and `present` and never *where*.

## A worked example

`plugins/genus-hostinfo` is a first-party plugin carried in this repo: host
thermal, GPU and memory state, which core deliberately does not ship because it
is a fact about one machine. It is a complete, installable reference — manifest,
entry points, schema, `read_only` declaration and tests.
