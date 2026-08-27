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

## Reloading without a restart

`systemctl reload robothor-engine` (SIGHUP) re-reads installed plugins in
place. Tools, schemas, guardrails, hooks and models all pick up the change
on their next use; runs already in flight are not disturbed. A plugin that
has been uninstalled is withdrawn by the same reload.
