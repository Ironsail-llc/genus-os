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
does not, and the difference is deliberate.

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
