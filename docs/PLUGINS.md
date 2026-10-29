# Writing a Genus plugin

Genus discovers extensions through Python entry points. There is no registry
to publish to and no package format to learn: if `pip` can install it, Genus
can load it.

## Starting a plugin

```bash
genus plugin new example-agent            # writes ./example-agent
genus plugin new example-agent --dir ~/src/example-agent
```

`genus plugin new <name>` writes a plugin repository that builds a correct
wheel as-is: a `pyproject.toml` publishing `genus.tools` and `genus.schemas`
entry points with `genus-plugin.yaml`, `agents/` and `workflows/` as package
data; `src/<package>/genus-plugin.yaml` declaring the example tool by name and
by entry point, plus the agent; one example tool handler and its schema; an
agent template bundle under `src/<package>/agents/<name>/`; contract tests; a
GitHub Actions workflow that runs them, builds the wheel and prints its
sha256; and a `CLAUDE.md` describing the layout. `<name>` is also the agent id,
so it follows the agent id rule (lowercase letters, digits, hyphens). It never
writes into a directory that is not empty.

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

**The contract version is checked and mismatches are refused.** This engine
speaks `1.0` and `2.0`. A plugin built against a different contract is not
loaded — an agent platform running third-party code that expects a different
tool-calling contract is a security problem, not a compatibility inconvenience.

`2.0` is `1.0` plus [composable patches](#composable-patches-append-and-detach)
(`append:`/`detach:`), `docs/webhooks.d/` drop-ins (instance files) and
`brain/agents/<agent>.d/` instruction fragments. Declare `contract_version: 2`
in `genus-plugin.yaml` and `genus_contract_version: "2.0"` in the payload when
your plugin uses any of them; engines before this release refuse it with the
contract-mismatch message instead of installing it without its hooks. A plugin
that uses none of them stays on `1.0` and still loads on older engines.
`genus plugin new` scaffolds `1.0`.

**A plugin cannot shadow a built-in.** Claiming `exec` or `write_file` is
refused and logged. That is a takeover, not an extension.

**Two plugins cannot claim the same name.** The second is refused rather
than silently overriding the first.

**Refusal is per-plugin and total.** A plugin whose names partly collide
loads none of them: half-loading leaves it in a state its author never
tested. And a plugin that fails to import is recorded and skipped — one
broken package must never stop the engine booting.

## State and shared primitives

A plugin's runtime dependencies are stdlib-only, and that rule is about
*third-party* packages. Importing `robothor.*` at runtime is fine: the plugin
runs inside the engine, and the primitives below exist so that it does not
reinvent them.

**`PluginState` — your plugin's tables.** A wheel cannot ship a database
migration, so every table a plugin needs is a *kind* of row in one core table,
`plugin_state`, keyed by tenant, plugin name, kind and key, holding a JSON
object:

```python
from robothor.plugins.state import PluginState

state = PluginState("acme")             # tenant defaults to the instance's
state.put("contact", "alice", {"stage": "lead"})
state.put("seen", message_id, {}, ttl_seconds=86_400)
state.get("contact", "alice")                     # {"stage": "lead"}, or None
state.exists("seen", message_id)
state.list("contact", prefix="a", limit=50)
# -> [{key, value, updated_at, expires_at}]
state.delete("contact", "alice")
```

Two plugins, or two tenants, using the same kind and key never see each
other's rows. A row written with `ttl_seconds` disappears from `get`, `exists`
and `list` the moment it expires. The engine deletes expired rows hourly for
the instance's tenant; rows for other tenants need
`PluginState.purge_expired()`. `list` returns rows in key order; its
prefix is literal text, not a pattern. The calls are synchronous database
queries: from an async handler, run them with `asyncio.to_thread`.

**`fleet_model_chain()` — never pin a model.** A plugin tool that calls a
model must not name one. The operator chooses the fleet's models, and a
pinned name goes stale the day they change them.
`robothor.engine.llm_client.fleet_model_chain()` returns the fleet's chain,
primary first, then the fallbacks, ending in the instance's model of last
resort when one is set. With no fleet defaults file it is that last-resort
model alone, and empty only when neither exists, so check for an empty list.

**`do_not_contact_phones()` — honour the opt-out.** Anything that texts or
calls a person must check the same opt-out the email tools do.
`robothor.crm.dal.do_not_contact_phones(phones, tenant_id)` returns the
numbers, exactly as you passed them, that belong to a person marked do not
contact. It compares the last ten digits, so a number written with a `+1`
country code, brackets, spaces or dots matches the same ten digits written
plainly; a number with fewer than ten digits matches nothing. The match
assumes NANP (US/Canada) numbers; an international deployment must normalise
both sides to E.164 before calling (nine-digit national numbers never match).
It checks the person's primary number, their additional numbers and their
SMS and voice identifiers. Drop every returned number before sending, and treat an
exception as a refusal: a lookup that failed is not a clean list.

The knowledge-base tools (`kb_*`) are engine tools, not imports: an agent
that runs your plugin's workflow lists them in `tools_allowed`.

## Checking what loaded

Plugin failures are logged at WARNING with the plugin name and the reason:

```
Plugin 'acme' declares contract '0.9', this engine speaks '1.0' or '2.0' — refused
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

**The reference channel plugin is [`genus-teams`](channels/teams.md)** — the
first channel to ship outside the engine, and the reason this group is no longer
a socket with nothing plugged into it. It is worth reading before you write your
own, because it exercises the parts of this page that an outbound-only channel
never touches: it contributes an authenticated FastAPI router through
`Channel.inbound_router` (the engine mounts it under `/api/channels/<name>`, and
only for an armed channel), it takes the runner through `bind_runtime` rather
than reaching into the engine for it, it runs inbound messages through the same
`channels.inbound` pipeline Slack uses instead of copying it, it implements
`ask` over an Adaptive Card bound to one conversation and one person (reached
through the platform's `ask_user` tool, which finds a plugin channel from the
`channel:<name>:<target>` trigger detail the shared pipeline records — no table
of channel names anywhere), and it contributes two `genus.doctor` checks. Its install verdict is `review` rather
than `safe`, which is the correct verdict for a channel: it reaches off the box
and it runs without a tool call, and an operator should see both before
accepting it.

**A receiving channel authenticates every request itself.** The engine skips
its own bearer for a `POST` to `/api/channels/<name>` or any path below it, and
only while that channel's router is mounted, because the senders such a route
exists for (a storefront, an SMS provider's webhook, a support form, Microsoft's
Bot Framework) never hold an engine token. Every other method on the channel's
path, and any name that is not mounted, still needs the engine bearer. So a
channel's `POST` handler must check its own credential (an HMAC over the raw
body, a shared token, a signed JWT) before it reads anything else, and refuse
with its own `401` or `403`. There is no flag that asks the engine to keep the
bearer in front of a channel: a `POST` route that cannot authenticate its
sender should not be in the router.

**Let the pipeline own ordering and memory.** `channels.inbound.handle_message`
serialises messages that share a `session_key`, runs different keys
concurrently up to `ROBOTHOR_CHANNEL_MAX_CONCURRENT_RUNS`, and records each
successful exchange in the session's history and the chat store, so it is
remembered across restarts. Ordering and the cap are per engine process. So pick a `session_key` per
conversation (per visitor, for a public chat) and call it from as many
concurrent handlers as you like. Pass `record_turn=False` for a message that
should not be remembered, and `history_limit=N` to hand the run only the last
N turns — a cheap bound on prompt size for a chat that can run long.

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
# Optional, and worth adding: the ENTRY-POINT names you publish into each
# group. An entry-point name is not a contribution name — genus-hostinfo
# publishes `hostinfo` and contributes `host_state` — so the two are declared
# separately. Declaring this is what lets `genus plugin install` compare your
# wheel's actual surface to your declaration exactly, before importing
# anything. Omit it and the comparison is only at group granularity, which
# caps the install verdict at `review`.
entry_points:
  genus.tools:
    - acme
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

**One exception, and it is deliberate.** A distribution `genus plugin install`
put there — one with a `source` on its lockfile row — is held to its declared
contribution names **whatever the mode says**. That grandfathering exists for
plugins published before manifests did, and a plugin that arrived through the
installer could not have been installed at all without a manifest the index had
pinned; letting it through `observe` would mean nothing compared declared names
to actual surface at any stage.

> **Editable installs (`pip install -e .`).** Their file list carries no
> package files, only a path shim, so the manifest beside the package is not in
> it. For a distribution whose `direct_url.json` says `editable`, and only for
> one, Genus locates the package through its import spec
> (`importlib.util.find_spec` on the entry point's top-level package, which
> finds the directory without executing anything) and reads the manifest from
> there. This is a development convenience; test under `enforce` with a real
> wheel before you publish.

## Shipping agents, workflows and workspace files

A plugin can carry agents, workflows, edits to an existing agent, and a few
other workspace files. They are declared in the same `genus-plugin.yaml` — so
`manifest_sha256` covers them, and a registry reads them without importing
anything — as paths **relative to the directory that holds
`genus-plugin.yaml`**, shipped as package data:

```yaml
# example_agent/genus-plugin.yaml
name: example-agent
contract_version: 1
handlers:
  - example_probe
entry_points:
  genus.tools:
    - example
agents:            # agent template bundles
  - agents/desk-monitor
workflows:         # copied to <workspace>/docs/workflows/<basename>
  - workflows/example-pass.yaml
patches:           # edit an EXISTING agent the plugin does not own
  - agent: main
    file: patches/main.patch.yaml
files:             # other workspace files, inside an allowlist
  - src: extra/webhooks.yaml
    dest: docs/webhooks.yaml
    mode: overwrite          # or if_missing
```

```toml
[tool.setuptools.package-data]
example_agent = ["genus-plugin.yaml", "agents/**/*", "workflows/*", "patches/*", "extra/*"]
```

| key | what it is | what happens to it |
|---|---|---|
| `agents` | a directory laid out like `templates/agents/core/main`: `manifest.template.yaml`, `instructions.template.md`, optional `setup.yaml` and `SKILL.md` | rendered by the agent-template installer — the same code as `genus agent install` — into `docs/agents/<id>.yaml` and the instruction file under the instance's `brain/`, and recorded in `.robothor/installed.yaml` with `source: plugin:<distribution>`. With no `setup.yaml`, the agent id is the directory name and the version is the plugin's. `.robothor/overrides/<id>.yaml` applies exactly as it does to any template |
| `workflows` | a file | copied to `docs/workflows/<basename>` |
| `patches` | `agent:` an agent id the plugin does **not** ship, `file:` a YAML file with top-level `install:` and `remove:` sections | each section has `set:` — a deep merge for mappings; any other value, lists included, replaces — `delete:`, a list of keys, dotted for nested ones, and `append:` / `detach:`, which add or take out single list items (see [Composable patches](#composable-patches-append-and-detach)). The order is `set`, `append`, `delete`, `detach`. A patch may not touch `id`. A patch whose target agent does not exist is skipped with a warning. **A list replaces**: `set: {tools_allowed: [x]}` on an agent with no `tools_allowed` (which means every non-opt-in tool) narrows it to `x`. `append:` does not help there either: it refuses to create or fill an absent or empty `tools_allowed` (or `heartbeat_tools_allowed` / `worker_tools_allowed`), and only adds `x` to an allowlist the agent already has. To give an unrestricted agent a plugin tool that is opt-in, append it to `tools_opt_in` |
| `files` | `src`, `dest`, `mode` (`overwrite`, the default, or `if_missing`) | `dest` must be under `brain/`, `docs/workflows/` or `docs/hooks/`, be exactly `docs/webhooks.yaml`, or be one `<name>.yaml` directly in `docs/webhooks.d/`. `if_missing` never replaces a file that exists. `brain/` has ownership rules, below (all of these are instance paths) |

### Composable patches: `append` and `detach`

`set:` owns a key wholesale, so only one plugin can `set: hooks` on an agent.
To let several plugins extend one agent — each giving `main` its own event
hook, instruction fragment or webhook channel — a patch names list **items**
instead:

```yaml
# example_agent/patches/main.patch.yaml
install:
  append:
    hooks:
      - stream: email
        event_type: email.new
        message: "A new email arrived; triage it."
    bootstrap_files: [brain/agents/main.d/example-agent.md]
    warmup.memory_blocks: [example_notes]
remove:                 # optional: without a detach, remove takes back what install appended
  detach:
    hooks:
      - {stream: email, event_type: email.new}
```

* **`append: {key: [items]}`** upserts each item into the list at `key` (dotted
  for nested), creating the list — and any missing mapping above it — when it
  is absent. A key that holds something other than a list is refused, and the
  patch is not applied.
* **Tool allowlists are never created.** An absent or empty `tools_allowed`
  (and `heartbeat.heartbeat_tools_allowed`, `worker.worker_tools_allowed`)
  means every non-opt-in tool, so appending to one would strip every other
  tool. `append:` refuses that, naming the key, and adds to an allowlist only
  where the agent already has a non-empty one. The same rule holds the other
  way: a `detach:` (or a removal) that would leave an allowlist empty is
  refused and the list is left as it is, so removing a plugin never widens an
  agent to every tool; the removal reports it, naming the key. Every one of
  these refusals is taken at plan time, before pip runs.
* **Identity.** A `hooks` item is identified by its `(stream, event_type)` and
  must have both (a `stream` may not contain `:`); an item of any other list (`bootstrap_files`,
  `warmup.memory_blocks`, `tools_allowed`, `tools_opt_in`, …) must be a scalar
  and is identified by its value. An item already present with the same
  identity is replaced in place, never duplicated, so a re-apply changes
  nothing.
* **`detach: {key: [items]}`** removes the entries whose identity matches and
  leaves the rest; an identity that is not there is a no-op. A `hooks` item in
  `detach` needs only `stream` and `event_type`.
* **Remove.** `genus plugin remove` applies the `remove:` section; when it has
  no `detach:`, the items the plugin last appended (recorded in the ledger) are
  detached for it. An upgrade that stops appending an item detaches it too,
  unless that would leave a tool allowlist empty: then the item is kept, the
  upgrade goes ahead with a warning naming the key, and the item is the
  operator's from then on (a later removal does not take it back).
  Detach removes every identity the plugin declared — including one that was
  already in the list before the plugin was installed (the original manifest is
  kept once as `.pre-<distribution>`).
* **Claims.** The ledger records each appended item as `hooks[<stream>:<event_type>]`
  or `<key>[<value>]`. Two plugins appending different items to the same list
  coexist; two appending the same identity are refused, as is one plugin
  appending to a list another `set`s or `delete`s (and the reverse).

A patch that uses `append:` or `detach:` belongs to a plugin that declares
contract `2.0` (see [The rules, and why](#the-rules-and-why)).

Two drop-in locations make the rest composable without a shared file:

* **Instruction fragments:** ship `brain/agents/<agent>/…` or
  `brain/agents/<agent>.d/<distribution>.md` as a `files:` entry (allowed for an
  agent the plugin patches) and `append` it to the agent's `bootstrap_files`;
  bootstrap files are read on every run.
* **Webhook channels:** ship `docs/webhooks.d/<distribution>.yaml` (same shape
  as `docs/webhooks.yaml`, a `channels:` mapping). The engine merges every
  `*.yaml` there, in name order, after `docs/webhooks.yaml`. Install refuses a
  drop-in that does not parse, has a `rate_limit_per_min` or
  `timestamp_tolerance_seconds` that is not a whole number, or declares a
  channel the base file or another drop-in already has. If a bad drop-in still
  turns up (an operator edit), the engine logs an error naming the file, skips
  it, and keeps serving the rest. `docs/webhooks.yaml` itself stays strict: a
  broken base file stops the engine from starting, as it always has.

### Ownership

A plugin only ever writes what is its own, or what its manifest says, in
reviewable form, it means to change — the rule the template installer's
instruction-file check already applies to agents.

* **Reserved ids.** No plugin may ship an agent called `main`, or any agent
  in the template catalog's `core` department (today `main` and `concierge`) —
  not even an exact copy of one. Change those through `patches:`, which the
  scan grades `review` and which is conflict-tracked between plugins.
* **Agents.** An agent id whose `docs/agents/<id>.yaml` already exists is
  refused unless `.robothor/installed.yaml` records it as this plugin's — with
  one exception. An **unrecorded** manifest whose parsed data equals what the
  plugin's bundle renders on this instance (comments, quoting and key order may
  differ), and whose instruction file has the same bytes, is a hand copy of
  exactly this agent: it is *adopted* — recorded as `source: plugin:<dist>` with
  `adopted: true`, and left byte-for-byte as it is. The next release that
  renders something different writes the plugin's version and keeps the copy
  once as `.pre-<distribution>`; `remove` retires it like any plugin agent.
  Recorded elsewhere, or different in any way (`main`, an operator-edited
  copy), it is somebody else's: never overwritten, never retired.
* **A plugin agent's instruction file** must be its own:
  `brain/agents/<id>.md`, or a file under `brain/agents/<id>/`. Any other
  `instruction_file` (the instance's `brain/TOOLS.md`, another agent's file) is
  refused before pip runs, even when the bytes there are identical.
* **`brain/agents/<id>.md` and `brain/agents/<id>/…`** belong to agent `<id>`.
  A `files:` entry may write there only when `<id>` is one of the plugin's own
  `agents`, or an agent it declares a `patch` for. That is how a plugin turns
  `main` into its persona: a patch for `main` plus
  `{src: files/main.md, dest: brain/agents/main.md}`; the original is kept as
  `main.md.pre-<distribution>`.
* **`docs/hooks/`** is executed by the engine (a `command` hook runs shell), and
  `docs/hooks/global.yaml` carries the platform's own hooks. An existing file
  there that the plugin did not write is refused in `overwrite` mode, with no
  exception; `if_missing` may seed one, and identical bytes are adopted (and
  stay protected). `docs/webhooks.yaml` stays overwritable — with a backup —
  and the plan marks it `[engine-loaded config]`.
* **Any other existing `brain/` file** on the instance the plugin did not write is refused in
  `overwrite` mode — the instance's `brain/TOOLS.md` and `brain/AGENTS.md` cannot be replaced —
  unless an agent the plugin ships or patches claims it as `instruction_file`.
  `if_missing` may seed one where it is absent.
* **Adopted files.** A destination that already holds the plugin's exact bytes
  — an installer copied it first — is *adopted*: recorded with
  `adopted: true`, not written, and not the plugin's. A later release with
  different bytes is judged as replacing someone else's file (refused for a
  shared instance `brain/` file; written with a `.pre-<distribution>` backup where the
  rules allow it), and `remove` leaves an adopted file, and any backup beside
  it, in place.
* **Files** another plugin wrote are refused.
* **Patch keys.** Each patched key (agent plus dotted key, prefix-aware) is
  recorded per plugin; a second plugin that sets or deletes a key another
  plugin already patches is refused, naming both. Appended list items are
  claimed one by one, by identity (see
  [Composable patches](#composable-patches-append-and-detach)).
* **Operator edits.** A file the plugin wrote and the operator has since edited
  is left alone by sync (with a warning) and by remove. That covers a plugin
  agent's manifest and instruction file as well as `files:` and `workflows:`:
  the ledger records what the plugin last wrote, so an edited one is kept while
  an unedited one still takes a new release. A manifest key some plugin's
  patch manages is not an operator edit. A kept edit is a warning, so
  `sync --strict` does not fail on it. To take the plugin's version, delete the
  file and sync again.

### Refusals

Taken before anything is written — for `genus plugin install`, before pip runs:
an absolute path or one with `..`; a source outside the plugin's package; a
destination outside the allowlist, or reached through a symlink anywhere on its
way; a `manifest.template.yaml` whose `id` is not the bundle's agent id; any
breach of the ownership rules above; a malformed patch. `--dry-run` lists every
patch with its keys and marks each file that would replace an existing one.

A rendered agent the engine itself would refuse at load — its manifest schema
under `ROBOTHOR_MANIFEST_SCHEMA_MODE=enforce` — is refused too. The template
installer's checks A–R are **reported, not enforced**, as they are for `genus
agent install`: they cannot see plugin tools, which are not in the static
registry.

Everything one plugin writes in one run is applied **all or nothing**. A
failure part-way restores every file that run touched and leaves the record of
the previous run in place.

### Install, sync and remove

**`genus plugin install`** writes them from the reviewed wheel, after pip and
after the lockfile row. `--dry-run` lists them under `workspace`.

**`genus plugin sync`** writes them for **every installed distribution that
declares them**, from the installed copy. This is the whole install path for a
plugin put on the import path by hand, which is how a container image without
pip gets its plugins:

```bash
uv pip install --no-deps --target "$ROBOTHOR_PLUGIN_DIR" ./example_agent-1.0.0-py3-none-any.whl
genus plugin sync
```

Sync applies the loader's own gate and nothing stricter: a plugin the loader
would refuse to import gets nothing written. `genus plugin sync` re-records the
lockfile rows first, so at that point a row's manifest digest always matches
and the filter that applies is **disabled**: `genus plugin disable <name>`
stops its files being written. In every manifest mode, `enforce`
included, a plugin the loader would load gets its declared files: its Python
already runs inside the engine, so letting it write declared, allowlisted
workspace files adds no privilege, and nothing undeclared is ever written.

One plugin failing does not stop the others. It is reported, its previous
files stay as they were, and sync exits `0`: the lockfile part, which every
other plugin depends on, did succeed.

A deploy job that needs to know uses **`genus plugin sync --strict`**: it does
exactly the same work, and exits `3` when any plugin's workspace files were
refused or failed (an unreadable `.robothor/plugin-assets.yaml` included),
naming them on stderr. A disabled plugin and one that has disappeared are not
failures. `2` still means the sync itself could not run.

```bash
genus plugin sync --strict || { rc=$?; [ "$rc" -eq 3 ] && echo "a plugin's files were refused"; exit "$rc"; }
```

Sync is idempotent, and safe to run after another tool has already copied the
same files or applied the same patch: identical content is adopted rather than
rewritten or backed up, a patch whose values are already present changes
nothing, and an existing backup is never overwritten.

A hand-copied agent manifest is adopted when its data matches (see
Ownership). One that differs is refused, and the whole plugin is skipped until
it is resolved: move the copied manifest **and** its instruction file
(`brain/agents/<id>.md`) aside, then sync — the refusal names both. An agent
the plugin already wrote is not re-rendered onto disk when nothing changed, so
a second sync touches no file.

**`genus plugin remove`** retires what the plugin wrote, from the record in
`.robothor/plugin-assets.yaml` (the wheel's patch files are gone by then):

* each agent's manifest moves to `docs/agents/retired/<id>.yaml` — the same
  retire the Helm does; nothing is unlinked, the instruction file stays — and
  its `installed.yaml` record goes. The ledger remembers the instruction file
  it left behind, so installing the same plugin again (or a release that ships
  the agent again) takes it back: an unedited file gets the plugin's version,
  and one the operator edited is kept, with a warning. An instruction file no
  record ties to this plugin, with bytes other than the plugin renders, is
  refused before pip runs;
* each workflow and file it wrote is deleted, unless it has been edited since,
  in which case it is left and named in the output;
* a file it overwrote is put back from its backup;
* a file it adopted is left where it is, with any backup beside it;
* each patch's `remove:` section is applied to its agent.

A distribution in `ROBOTHOR_PLUGIN_DIR` is removed **without `--force`**, even
one only `sync` recorded: that directory belongs to the platform. Its files are
deleted from that directory using its own `RECORD` (pip is not involved — it
cannot see a `--target` install, and the engine image does not ship it), so the
next `sync` does not find it again.

Everything that can refuse — an unreadable `.robothor/plugin-assets.yaml`, for
one — is checked before anything is deleted, so a refused remove leaves the
plugin, its lockfile row and its files as they were. A remove that stopped
after deleting the package is finished by running it again: no pip, no
`--force`.

**A plugin that disappeared** — no longer on the import path at the next
`sync` — is **reported, not retired**. Discovery reads the interpreter's path,
and a sync run from a shell missing the plugin directory would otherwise retire
every plugin's agents at once. Retire one on purpose with:

```bash
genus plugin remove --force example-agent
```

which, for a distribution that is no longer installed, skips pip and retires
its workspace files.

### Upgrades and backups

Upgrading re-renders the agents from the new templates with the operator's
`.robothor/overrides/<id>.yaml` applied, so a new release updates the agent and
keeps the operator's settings. Anything the new release no longer declares is
retired as a removal would. A patch is re-applied from the new `install:`
section; a key an older release set and the new one does not mention stays
until the plugin is removed.

The first time a plugin overwrites a file it did not create — a `files:`
destination, an agent it patches — the original is kept beside it as `<file>.pre-<distribution>`. Only
once, only when the content actually changes, and an existing backup is never
replaced.

Patches go through the same comment-preserving round trip `genus agent` uses,
which keeps comments only when `ruamel.yaml` is installed (the platform does
not depend on it). Without it the patched manifest is rewritten by PyYAML and
its comments are lost; the `.pre-<distribution>` backup is the original.

### Scan verdict

A wheel whose manifest declares **any** workspace asset is graded `review`,
never `safe`, with one reason per kind, read from the declaration normalised
the way the installer reads it (`./brain/x` is `brain/x`, an instance path):

| declared | why it is `review` |
|---|---|
| `agents` | a rendered manifest can declare lifecycle hooks, including `command` hooks the engine runs as shell |
| `workflows`, `files` → `docs/workflows/` | the engine runs them |
| `files` → `docs/hooks/` | the engine executes them |
| `files` → `docs/webhooks.yaml`, `docs/webhooks.d/` (instance drop-ins) | configuration the engine loads |
| `files` → `brain/` (instance) | text a model reads |
| `patches` | they change an agent the plugin does not own |

A packaged `.yaml` is otherwise inert data to the scan, which is why the
declaration, not the member list, is what it reads here. `genus plugin install`
needs `--accept-review` for any of these. `genus plugin sync` runs no scan; the
ownership rules above are what bound it.

Adopting a hand-copied agent does not raise the verdict further: whether a copy
exists is a fact about the workspace, not the wheel, every wheel that ships an
agent is already `review`, and the agents that matter most are reserved.

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
      "recorded_at": "2026-09-15T00:00:00+00:00",
      "dist_sha256": "…",
      "members_accounted": 14,
      "source": {
        "origin": "registry",
        "index_url": "https://ironsail-llc.github.io/genus-plugins/index.json",
        "publisher_key_id": "genus-2026",
        "installed_at": "2026-09-15T00:00:00+00:00"
      }
    }
  ]
}
```

`verdict` is `unscanned` for anything `genus plugin sync` merely *found*
installed — nothing scans a distribution that is already a directory tree, and
a field reading `safe` because no scanner ran would be worse than no field at
all. A plugin that arrived through `genus plugin install` carries the verdict
its wheel actually got.

`dist_sha256`, `members_accounted` and `source` appear only for plugins this
platform installed. Their **absence** is information: a row without a `source`
is one an operator pip-installed by hand, and `genus plugin remove` refuses
those without `--force`; `members_accounted` is how many wheel members the scan
classified when the row was written, which is what makes the `verdict` beside it
a measurement rather than a claim. All three are additive — a lockfile written
before they existed parses unchanged.

### The verbs

| command | what it does |
|---|---|
| `genus plugin list` | what is installed, what it contributes, what is disabled, what was refused |
| `genus plugin info <name>` | one distribution: manifest, groups, contributions, lock row, where it came from |
| `genus plugin install <name\|wheel>` | install from a signed index, or from a wheel you hashed yourself |
| `genus plugin remove <name>` | uninstall a plugin this platform installed and drop its row |
| `genus plugin sync [--force]` | upsert a row per installed distribution; drop rows for ones that are gone |
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
`genus plugin sync` is what turns the control on.

### When the file is damaged

Two different cases, and they are handled differently on purpose:

* **The whole file** does not parse, is not even decodable text, or cannot be
  read at all. It is treated as absent — logged once, reported by
  `plugins.lockfile`, and nothing is refused. A governance file that could
  brick an engine would be deleted by the first operator it bricked.
* **One row** does not parse (not an object, or missing `name`). The rows that
  *do* parse still govern — discarding them would put every other disabled
  plugin straight back into service — and the unreadable ones are counted,
  logged once with their position, and reported by `plugins.lockfile`. They are
  never dropped in silence.

In both cases `genus plugin sync` **refuses**, because it carries `enabled`
forward from what it reads and a file it cannot read is one whose disables it
would erase. `--force` rebuilds from what is installed and accepts that loss —
it reports how many rows it could not read, names any disable it could still
make out, and keeps the old bytes as `plugins.lock.rejected` so nothing is
destroyed without a copy. The doctor points at `--force` and what it costs
rather than at a plain `sync`.

A third case is not a lockfile problem at all: if the *path* cannot be read (it
is a directory, or permissions deny it), `sync`, `enable` and `disable` report
an I/O error — exit 2 on the CLI, **503** over HTTP — rather than offering
`--force`, which cannot fix a filesystem.

### The doctor

| check | severity | what it asks |
|---|---|---|
| `plugins.lockfile` | recommended | present, parseable, every row readable, mode 0600 |
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
| `POST /api/plugins/sync` | `POST /api/admin/plugins/sync` | record what is installed; **409** when the existing file's contents cannot be read (`--force` is CLI-only), **503** when the path itself cannot be written |
| `POST /api/plugins/{name}/enable` | `POST /api/admin/plugins/{name}/enable` | flip the row; 404 if unrecorded; does **not** reload |
| `POST /api/plugins/{name}/disable` | `POST /api/admin/plugins/{name}/disable` | as above |
| `POST /api/plugins/reload` | `POST /api/admin/plugins/reload` | runs the SIGHUP body; returns `{generation, loaded, failures}` |
| `POST /api/plugins/install` | `POST /api/admin/plugins/install` | `{name, version?, index?, accept_review?, dry_run?}` → the install plan and its verdict; **422** for a blocked or unreviewed wheel, an unverifiable index, or a `name` that is not a distribution name |
| `POST /api/plugins/{name}/remove` | `POST /api/admin/plugins/{name}/remove` | `{force?}` → `{name, removed, row_dropped, reload_hint, note}` |

The listing also answers **`indexes`** — the index URLs this instance reads, in
order, **exactly as configured**, including one the install route would refuse.
An install form offers a *choice* between them rather than a free-text URL box,
because a browser that can type any URL is a browser that can make the engine
fetch on a caller's say-so. Filtering to what is *usable* is the consumer's
job: `install` refuses a non-`https` `index` with a 422, and a listing that
dropped one silently would leave an operator with a setting that has no effect
and no explanation. (The Helm filters, and names what it left out.)

Note that **omitting `index` is not the same as naming the first one**:
`load_indexes` reads every configured URL and is all-or-nothing, so a single
unusable entry refuses the whole set. A caller that has filtered the list should
name the index it chose rather than leave the field out.

The listing's `lockfile` block answers `path_configured`, `present`, `malformed`,
`rows` and **`problem`** — the sentence the CLI and the doctor print, or `null`
when the file is fine. `malformed` says *that* the whole file is unusable;
`problem` says *which* fault, and there are **five**, with different remedies:

| `problem` | `malformed` | what it means |
|---|---|---|
| `cannot be read (<OSError>)` | `true` | the PATH will not read — a directory, a permission denial. `--force` cannot fix a filesystem |
| `is not readable text (<error>)` | `true` | the bytes are not decodable |
| `is not valid JSON (<error>)` | `true` | the file does not parse |
| `does not hold a 'plugins' list` | `true` | it parses, but it is not a lockfile |
| `holds N row(s) that cannot be read (position(s) …)` | **`false`** | the file parses and the readable rows still govern; the unreadable ones are decisions that cannot be honoured |

The last one is the reason `problem` must be read **independently of
`malformed`**: it is the only signal that some rows govern nothing, `rows`
counts only the readable ones, and every other field looks healthy.

Each plugin row carries **`source`** — the lock row's origin block, or `null`
for anything this platform did not install, which is the only thing that
distinguishes a plugin `remove` will act on from one it refuses.

#### Three namespaces, and the field that keeps them apart

A reload answers `failures[]` of
`{name, group, reason, distribution}`, and the first and last of those are
**different namespaces**:

| namespace | example | where it appears |
|---|---|---|
| distribution name | `genus-hostinfo` | `plugins[].name`, `failures[].distribution`, `enable`/`disable`/`remove` paths |
| entry-point name | `hostinfo` | `failures[].name` |
| contribution name | `host_state` | `manifest.declared` values, the keys of a loaded payload |

`manifest.declared` holds **contribution** names. It therefore says nothing
about which entry point a distribution publishes, and must never be read as a
denial that it owns one. `failures[].distribution` is the join key: the loader
has `ep.dist` in hand when it records the refusal, and `null` means only that
the metadata layer could not name the distribution.

No response carries a filesystem path. Which distributions are installed is a
platform fact; where an instance keeps its files is not, so the listing answers
`path_configured` and `present` and never *where*, `problem` is built from the
error class rather than the file — and the install response omits the pip
command, which holds a temp directory and the interpreter's location. The CLI
prints it; HTTP does not.

`install` takes a distribution **name** and nothing that could become a path or
a URL. A dashboard naming a filesystem path would be a file read on the
engine's box; one naming a URL would make the engine fetch on a caller's
say-so. Installing a wheel from disk is a CLI-only act, where the operator is
standing at the machine.

## Installing from the registry

The registry is not a server. It is a signed static file — `index.json` plus a
detached `index.json.sig` — that anything can host: GitHub Pages, an S3 bucket,
a company's own nginx, or a directory copied onto a box with no network.
Trust comes from the Ed25519 signature and the public key you pinned, never
from the host, so every mirror is exactly as trustworthy as the original.

```bash
# the newest published version
genus plugin install genus-hostinfo
# an exact version
genus plugin install genus-hostinfo==0.1.0
# the plan, and write nothing
genus plugin install genus-hostinfo --dry-run
```

What happens, in order, each step a refusal:

1. **Resolve** the name in every index `ROBOTHOR_PLUGIN_INDEXES` lists, in
   order. First index publishing the name wins — so a company index listed
   first shadows the platform's, on purpose. A name published by **two
   different publishers** is refused until you name one with `--index <url>`.
2. **Verify** the index's signature against a key you pinned. An index is
   also refused when its `key_id` is unknown, its `schema` is not 1, it is
   dated in the future, it is more than 90 days old (a mirror serving a
   pre-yank index would undo the yank silently), it redirects off its origin,
   it exceeds 1 MB, or its bytes are not the canonical form that was signed.
3. **Download** the wheel to a temp directory and check its sha256 against
   what the index signed. Size-capped at 50 MB.
4. **Open** it under bounded zip rules: no symlink member, no absolute or
   traversing path, no duplicate names, member and uncompressed-size caps —
   all checked against the headers before a byte is written.
5. **Compare** the wheel's `genus-plugin.yaml` to the `manifest_sha256` the
   index signed. A declaration widened after publication is refused here,
   rather than caught by the lockfile's drift check one import too late.
6. **Scan** it (below). `blocked` refuses; `review` refuses unless you pass
   `--accept-review`; `safe` proceeds.
7. **Install** with pip — and pip is allowed to do nothing at all:
   `--no-deps --no-index --find-links <the temp directory>`. It resolves
   nothing, reaches nowhere, and installs exactly the one file that survived
   the steps above. It never sees a URL, an `--index-url` or `--pre`. The
   engine's interpreter runs it as `python -m pip` when it has pip; a venv
   built by uv (the container image's) has none, so the installer runs
   `uv pip install --python <that interpreter>` with the same flags instead.
   With neither pip nor `uv` on PATH, the install is refused with a sentence
   naming both. Either way it reinstalls without a cache (`--force-reinstall
   --no-cache-dir` for pip, `--reinstall --no-cache` for uv), so installing a
   rebuilt wheel of a version already installed replaces its files, and what
   lands on disk is the file whose hash was checked, never a cached copy of
   the same name and version. `genus plugin remove` uninstalls through the same choice
   (`uv pip uninstall --python <that interpreter>` without pip). With neither,
   it refuses; `--force` then retires the plugin's workspace files and drops
   its lockfile row without uninstalling the package.
8. **Record** the row: verdict, wheel hash, and where it came from.

Nothing signals the engine. The running daemon keeps serving the plugin set it
discovered until you reload it (SIGHUP, or `POST /api/plugins/reload`).

### `--no-deps` is permanent, and it constrains you

A plugin's dependencies are **not** installed. An installer that resolved them
would let a plugin's own metadata name the next download, which is the supply
chain this design exists to close.

So a plugin that needs a library the platform does not already ship must either
vendor it, or ask for it to be added to the platform's extras. That is a real
constraint on plugin authors and it is deliberate.

### Pinning a publisher's key

`robothor/plugins/registry_keys.py` ships **empty**. The production key for the
Genus registry is minted by whoever runs that registry; a placeholder committed
before it exists is a placeholder nobody replaces.

To trust a publisher — your own company's internal registry, say — drop their
PEM **public** key into a directory and point `ROBOTHOR_PLUGIN_INDEX_KEYS` at
it. The file's stem is the `key_id` the index must name:

```bash
mkdir -p /srv/app/plugin-keys
cp acme-2026.pem /srv/app/plugin-keys/       # key_id "acme-2026"
genus config set ROBOTHOR_PLUGIN_INDEX_KEYS /srv/app/plugin-keys
genus config set ROBOTHOR_PLUGIN_INDEXES \
  "https://plugins.acme.example/index.json,https://ironsail-llc.github.io/genus-plugins/index.json"
```

Publishing to your own registry is `scripts/build_plugin_index.py`: it reads a
directory of wheels, takes every field out of the wheel itself, runs the same
scanner, signs the canonical bytes, and verifies its own output with the
shipped parser before it reports success.

```bash
python scripts/build_plugin_index.py dist/ \
  --out index.json --key ~/.keys/acme.pem --key-id acme-2026 \
  --publisher acme --base-url https://plugins.acme.example/wheels/
```

### The index also publishes agent bundles

An entry declares a `kind`: `plugin` (the default, and what every entry written
before this existed is) or `agent-bundle`. One signed document, one signature,
one set of pinned keys — an operator who already trusts a publisher's wheels
should not have to pin a second key for their agents.

```json
{
  "kind": "agent-bundle",
  "name": "triage-bot",
  "version": "1.2.0",
  "requires": {"plugins": ["genus-billing"], "adapters": [],
               "secrets": ["BILLING_API_KEY"], "skills": ["triage"]},
  "artifacts": [{"kind": "bundle", "filename": "agent-triage-bot-1.2.0.tar.gz",
                 "url": "https://plugins.acme.example/agents/agent-triage-bot-1.2.0.tar.gz",
                 "sha256": "…", "size": 4096}]
}
```

A plugin entry must carry a `wheel` artifact and a bundle entry a `bundle`
artifact; an entry that contradicts itself is refused at parse time. `requires`
and `scan` are read out of the bundle's own contents by the builder — the same
rule that keeps a publisher from typing a plugin's groups by hand — so the plan
an operator reads before installing is the one the signature covers.

**Bundles are scanned like wheels.** `build_plugin_index.py` records a
`{verdict, reasons, scanned_at}` for every bundle and refuses to sign a
`blocked` one without `--allow-blocked`, exactly as it does for a wheel the
static scanner blocked. The installer re-runs the scan on what it actually
downloaded — the publisher's verdict is advisory for a bundle for the same
reason it is advisory for a wheel. `blocked` (a credential literal, a foreign
home path) always refuses; `review` (a tool that acts on the world, an absent
`tools_allowed`, `can_spawn_agents`) needs `--accept-review`. Details in
`docs/AGENT_BUILDER.md` §8a.

The delivered body is also checked against the `size` the index signed, not only
against the 50 MB cap: a size a signed document declares and nobody verifies is
a field that means nothing.

`build_plugin_index.py` picks up `*.tar.gz` alongside `*.whl` and detects the
kind from `bundle.yaml`. Drop both in one directory and sign them together.

**The two verbs do not take each other's entries.** `genus plugin install` on an
agent bundle, or `genus agent install` on a plugin, is refused with the verb
that *does* take it — not with "not found", which would send you hunting for a
publishing mistake that is not there. See `docs/AGENT_BUILDER.md` §8a for the
agent side.

## Installing a wheel offline

No index, no network — a wheel on disk and a hash you obtained some other way:

```bash
genus plugin install ./genus_hostinfo-0.1.0-py3-none-any.whl \
  --sha256 3b1f…c0de
```

`--sha256` is **required**. There is no signed index vouching for a file you
name yourself, so the hash is the only thing that says it is the file you
meant. Everything from step 4 onward is identical, and the lock row records
`"origin": "wheel"` with no index or key.

This is CLI-only. The HTTP route takes a distribution name and nothing that
could become a path.

## What the scanner refuses, and why

The scan runs on the bytes actually downloaded, before pip is allowed near
them. It is offline, deterministic, and an **AST walk, never a grep**: a
docstring reading "never call `eval()`" must not block an install, and
`getattr(builtins, "ex" + "ec")` must. A text scan gets both backwards, and a
scanner that cries wolf is one whose verdict gets waved through every time.

### Every member is accounted for

The scan classifies **every file in the wheel**, not just the Python. Code is
parsed, prompt text is named, inert data (yaml, json, markdown, text, images,
fonts) is allowed *by type*, and **anything else is refused with the file
named**. This installer accepts pure-Python wheels only.

That rule exists because the first version of this scanner had no such rule,
and four wheels whose entire payload was a non-`.py` file came back `safe` with
zero reasons — including a `.pth`, which `site.py` executes at **every**
interpreter start, before the loader, before the manifest gate, and before a
`enabled: false` row is ever read. The plugin never had to load, or even be
enabled, to run.

**`blocked` — the install cannot proceed at all:**

| finding | why |
|---|---|
| a `.pth` file anywhere | `site.py` runs it at every interpreter start, before anything can refuse it |
| a `.so` / `.pyd` / `.dylib` / `.exe` | nothing here can read machine code |
| a `.sh` / `.ps1` / `.js` / other non-Python script | same, and it is not what a plugin contributes through |
| anything under `*.data/scripts/`, `*.data/data/` or `*.data/headers/` | pip installs these OUTSIDE the package — onto `PATH`, under `sys.prefix` |
| a member shipping with the execute bit set | a plugin contributes through entry points, never as a program |
| any other file type the scan cannot read | "we did not look" is never `safe` |
| two `genus-plugin.yaml` files | which declaration the engine would enforce is ambiguous |
| a `genus.*` group the manifest declares nothing for | undeclared surface: the loader would import it before anything could compare the two |
| an entry point the manifest's `entry_points:` does not name | undeclared surface, exactly |
| a manifest claiming a built-in name | shadowing `exec` or `web_fetch` is a takeover, not an extension |
| a `contract_version` this engine does not speak | third-party code expecting a different tool-calling contract |
| `os.system`, `os.popen`, `os.exec*`, `os.spawn*` | a program is executed directly |
| any `subprocess.*` call | a plugin runs inside the daemon; spawning is outside every guardrail applied to it |
| `shell=True` | a string handed to a shell |
| `eval` / `exec` / `compile` on non-literal input | what runs cannot be read |
| `eval` / `exec` / `compile` on a **literal** whose code is itself refused, or which will not parse | a literal is not safe because it is readable — it is safe only if something reads it, and now something does |
| `pickle` / `marshal` / `shelve` / `dill` loading anything but a literal | deserialising executes whatever the bytes say, by design |
| a decoder (`b64decode`, `unhexlify`, …) feeding `exec` | code hidden from review inside encoded data |
| **dynamic name resolution** — a non-literal `getattr`, `__import__` or `importlib.import_module`, or a `globals()` / `locals()` / `vars()` lookup | a name this cannot read is a name it cannot judge |
| `import ctypes` / `cffi` | native code outside every guardrail the engine applies to Python |
| a raw socket | egress the engine's rules never see |
| a write under `/etc`, an SSH directory, or a credentials path | persistence and credential theft |

The code rules follow **names, not spellings**. A per-module binding table is
built from the imports and straight-line assignments, so
`from os import system`, `import subprocess as s`, `builtins.exec`, `e = exec`,
`__import__("os").system` and `importlib.import_module("sub" + "process").run`
all reach the same rule as the literal spelling. Eight one-line renames of that
kind used to grade `safe`.

**`review` — refused unless you pass `--accept-review`:**

- it contributes to `genus.hooks`, `genus.guardrails`, `genus.sandboxes`,
  `genus.channels`, `genus.memory` or `genus.jobs` — these run with no tool
  call, on a schedule or on every turn, so installing one changes what the
  engine does by itself
- it imports `requests` / `httpx` / `urllib` — it reaches off the box
- it imports `os`, `subprocess`, `socket`, `importlib`, `shutil`, `pty`,
  `multiprocessing`, `pickle`, `marshal`, `shelve` or `dill` — named with the
  line, whatever the call sites look like
- it calls `exec` / `eval` / `compile` on a string literal that *was* parsed and
  scanned and came back clean — code arriving as data is worth an eye
- it reads `os.environ` directly rather than through the settings accessor
- it ships prompt text — a `*.prompt` file, a `SKILL.md`, an `instructions*`,
  or anything under a `skills/`, `prompts/` or `instructions/` directory.
  A plain `README.md` is documentation and does **not** count: matching every
  `*.md` meant a plugin that merely vendored one needed `--accept-review`
  forever, which is how a verdict stops meaning anything
- its manifest declares no `entry_points:`, so the surface is only compared at
  group granularity
- it publishes more entry points into a group than the manifest declares names
  for
- **anything the scan could not read**: a file that would not parse, one over
  the size cap, or a wheel with more files than the scan bound

Every reason names a `file:line` inside the wheel, and the count of classified
members is reported where an operator can compare it to the wheel — printed by
`genus plugin install --dry-run`, carried as `members_accounted` in the install
response, and recorded on the lockfile row — so the claim is checkable rather
than asserted.

**Expect `review` to be the common verdict.** `safe` means "contributes tools,
and touches nothing outside this process" — a genuinely narrow plugin. Anything
that imports `os` is `review`, and that is the intended shape: the operator
says yes once, having read what they are saying yes to.

### Prompt text and `--scan-prompts`

Prompt text is reported as `static-only` by default: the bytes were noticed,
not read. `--scan-prompts` runs the platform's existing injection screen over
the wheel's prompt text *and* its documentation, and a finding becomes a
`review` reason — so on a wheel that would otherwise be `safe`, the flag can
change the verdict.

Two honest caveats. The screen is the one built for **assembled agent prompts**,
so it is noisy over ordinary operations documentation: measured over five real
documents it flagged three, including a runbook containing `rm -rf /var/cache/…`
and a scheduling doc containing `cron('0 3 * * *')`. And its findings are never
`blocked` — they are reasons under a `review`. A screen that cannot run at all
reports `static-only` with a sentence rather than reporting clean.

### What the scanner cannot see

The binding table follows names through imports, aliases, straight-line
assignment, walrus bindings, class attributes and `functools.partial`. It is a
**lower bound on what the code can reach, not a proof**, and the honest list of
what still gets past it is short and worth knowing:

- **Indirection through data.** A list or dict holding `os.system`, indexed at
  the call site; `self.run = os.system` set in `__init__`; passing a dangerous
  callable as an argument to something else. These come back `review` rather
  than `blocked` — the import is named, the call is not.
- **Reflection the table cannot follow**, such as
  `operator.attrgetter('system')(os)`.
- **`types.FunctionType(compile(...))`** and similar constructions of a callable
  from parts.
- **Anything decided at run time** by control flow the scan does not execute.

Two things bound the damage. The modules those tricks have to reach through —
`os`, `subprocess`, `socket`, `importlib`, `pickle` and friends — are all
`review` reasons on the import alone, so a wheel using any of them stops and
asks. And the verdict is re-computed by the installer on the bytes it actually
downloaded, so a publisher's own `safe` is never taken on trust.

**The scan is not a sandbox and does not claim to be.** A plugin that passes
still runs with the daemon's privileges once it is imported. What the scan buys
is that hiding something costs effort, and that what it finds gets named.

The full design and what was deliberately left out is
[`docs/rfcs/0002-plugin-distribution.md`](rfcs/0002-plugin-distribution.md).

### From the Helm

**Settings › Plugins** drives every route above, and it is what makes the
lockfile reachable on a box nobody has a shell on.

**Install from the registry** sits at the top. Type a distribution name (and
optionally a version; the index is a picker when more than one is configured)
and press **Preview**, which posts `dry_run: true` and renders the plan: the
artifact, the first twelve characters of its sha256 with the whole hash on the
clipboard, the publisher key id that signed the index, the groups it
contributes to, how much was scanned, and every reason sentence in full. The
verdict is a **word** — `safe`, `review` or `blocked` — and never a checkmark,
because what the scanner offers is a narrow claim about source it did not
execute and a tick beside a package name reads as a clearance.

`review` is the ordinary outcome, not an error: anything importing `os`, or
whose manifest omits `entry_points:`, lands there. So **Install** is dead until
the operator ticks *I accept the review findings*, which is what becomes
`accept_review: true`. A `safe` plan needs no tick. A `blocked` plan gets **no
Install button at all** — not a disabled one, which is a control that looks
like it might work if you tried harder — and says so in as many words. Every
refusal from the route is printed as the server's own sentence; a **504** in
particular says the operation may still be running, which is not the same thing
as failed.

The form takes a name and never a path or a URL. To install a wheel you have on
disk, use `genus plugin install ./x.whl --sha256 …` on the box; the card says
so rather than leaving you to discover it.

Below that, **Record installed plugins** when no lockfile exists yet — the
switches are dead until then, because a toggle with no row to flip is a 404 —
and one card per distribution: its state (`loaded` as a fact, `disabled` as a
decision, `failed` as a fault), a drift badge carrying the engine's own
sentence, the entry-point groups, what it contributes as `kind × n`, its
manifest, and — for a distribution this platform installed — where it came
from and a **Remove** button. Remove confirms inline beside the card, and it
appears **only** where the lock row carries `source`: `remove` refuses a row
somebody pip-installed by hand, so a button on every card would answer 422 on
most of them, and `--force` stays on the CLI.

A reload's failures name ENTRY POINTS, not distributions, and the page files
each under `failures[].distribution` — printing `group/entry-point` beside the
line so the match can be checked. What the engine could not name, the page does
not name either: it goes to the unattributed list with its group and entry
point, for the operator to place.
Turning one off writes the row and nothing else, exactly as `genus plugin
disable` does, so the card says the engine is still running it and a bar offers
**one** reload after however many toggles; `disabled by operator` comes back in
that reload's failure list and is rendered as the operator's own decision, not
as something to fix. Removing does not unload either: the files go now, the
modules the engine already imported stay until the same bar's reload.

Two things it deliberately does not do. It never renders a **lock row's**
`verdict`: a row recorded by `sync` says `unscanned` — nothing looked at it —
and a word on the screen where third-party code is turned on reads as a
judgement. (The install card is the opposite case and shows its verdict in
full: that one is a measurement of the exact bytes about to be installed.) And
it offers no `--force`, pointing at `genus plugin sync --force` on the box
instead — and only when the engine's own refusal names it.

The page renders `lockfile.problem` — the same sentence the CLI and the doctor
print — **whenever it is non-null**, and the two faults get different cards
because they have opposite consequences:

- **`malformed: true`.** The file governs *nothing*: `usable = present and not
  malformed`, and the loader opens with `if not lock.usable: return None`. The
  card says every plugin that was turned off is loading again and sends the
  operator to Record, whose refusal distinguishes damaged contents (409) from an
  unwritable path (503).
- **`malformed: false` with a `problem`.** Unreadable *rows*. The readable ones
  still govern, so this is a warning rather than an alarm — but whatever the
  unreadable rows turned off is loading right now and the page cannot say which
  plugins those were, so it says that instead of showing a row count and a clean
  bill. The header chip says so too.

Neither card prescribes `--force`: the page offers it only when the server's own
refusal names it, since a 409 also answers "no lockfile path resolves", where
there is nothing to force.

## A worked example

### Installed payload verification

`robothor.plugins.installed.verify_installed_wheel` compares an installed
distribution with a separately fingerprinted wheel and its enabled governance
record. It checks payload bytes, declared files, extra package members and
symlinks without importing plugin code. The wheel is captured before inspection
so replacing the input during verification cannot substitute different bytes.
Installer metadata and caches corresponding to declared Python sources are
allowed; cache contents and already loaded Python objects are not attested.
Relocated `.data` layouts and shared namespace packages need separate support.
This is a verification primitive, not an installation or runtime readiness API.

Managed sales runtime readiness combines this primitive with cold startup
identity and native service-registry origin checks. It pins the governance file,
installed-file history and plugin generation. Reinstalling, restoring edited
files or reloading plugins requires a fresh engine process before managed work
can resume. This integrated path currently supports service-only fleet plugins.

Keep source-only build and deployment commands out of runtime plugin wheels.
Native installation scans the actual wheel; bundling a build command can cause
an otherwise inert adapter to be refused for its subprocess capability.

`plugins/genus-hostinfo` is a first-party plugin carried in this repo: host
thermal, GPU and memory state, which core deliberately does not ship because it
is a fact about one machine. It is a complete, installable reference — manifest,
entry points, schema, `read_only` declaration and tests.
