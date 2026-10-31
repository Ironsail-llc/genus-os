# Models

Genus OS talks to language models through one registry,
`robothor/engine/model_registry.py`. Every id the platform will dial is declared
there with its context window, output ceiling, per-token prices and whether it
is a reasoning ("thinking") model. Anything not declared falls back to
conservative limits and is not offered anywhere a person picks a model.

## The stack today (2026-09-11)

| Role | Model id | Notes |
|---|---|---|
| Fleet primary (shipped default) | `openrouter/xiaomi/mimo-v2.5` | 1M context, tools, reasoning, cheapest cached input |
| Candidate primary | `openrouter/deepseek/deepseek-v4.1-flash` | 1M context, tools, reasoning, image input; launched 2026-09-10 |
| Value alternative | `openrouter/z-ai/glm-5.3-flash` | 1.3M context, tools, reasoning; verbose by default |
| Budget worker | `openrouter/~deepseek/deepseek-v4-flash-latest` | floating alias — never a primary for an interactive agent |
| Escalation | `openrouter/xiaomi/mimo-v2.5-pro`, `openrouter/deepseek/deepseek-v4-pro` | judge and hard tasks |
| On-device tier | `ollama_chat/qwen3.8:27b` | appended to every chain; the only defence against a capped key |

The instance's actual default lives in `docs/agents/_defaults.yaml` (instance
data, not in git): `model.primary` and `model.fallbacks`. Change it there, or
with `scripts/set-fleet-model.py`, and restart the engine — or use **Save
default** on the dashboard's Providers page, which has the engine write the
file into its own manifest directory and reload it, with no restart. A per-chat override
(`chat_sessions.model_override`, set by the Telegram `/model` picker) beats the
manifest for that chat until it is cleared.

## Status of an entry

Each registry entry carries `status`:

- `current` — offered in every picker, recommended.
- `legacy` — still valid and resolvable, offered after the current ones, not recommended for new agents.
- `deprecated` — resolvable so old manifests keep loading, withheld from every picker; the manifest validator warns and names `replaced_by`.

Ids that disappear from the provider are marked `deprecated` the day it is
noticed (2026-09-11: `openrouter/stealth/ox-alpha` and `openrouter/xiaomi/mimo-v2-pro`
left OpenRouter). Do not delete an entry: an agent manifest may still reference it.

## Reasoning models and the client library

The OpenRouter client (litellm) only forwards the `thinking` parameter for models
its own bundled table marks as reasoning-capable. A newly released model is not
in that table, so a registry entry with `supports_thinking: True` would make
every call fail with `UnsupportedParamsError` until the library ships an update.
`register_pricing_with_litellm()` therefore publishes `supports_reasoning` for
every thinking model in the registry, unconditionally; the test suite asserts it
for each such entry. Register the model here and it works the same day.

### OpenAI reasoning models

A reasoning model on the OpenAI API itself (`openai/gpt-5…`, an o-series model,
or the same id without the `openai/` prefix) takes a different request shape.
The engine sends it `reasoning_effort` instead of the `thinking` block, which the
client library refuses for OpenAI, and sends no `temperature`, which OpenAI
rejects for these models. The effort follows the agent's own reasoning-effort
setting: `low`, `medium` and `high` pass through, `max` becomes `xhigh` only
where the library's catalog says the model offers it (otherwise `high`), and the
re-ask after a reasoning-only reply uses `low`. The model is recognised as
reasoning from its registry entry or, failing that, the library's catalog, so a
model with no entry still gets the right shape. OpenRouter's `openrouter/openai/…`
routes and `codex/…` are unaffected.

OpenAI accepts at most 128 tools on one request. An agent with broad tool access
can exceed that; deferred tool loading (`ROBOTHOR_RIP_16_ENABLED`) keeps the
advertised set small.

## Prompt caching

Providers cache a request's prefix, so whatever changes from one run to the next
has to come after whatever does not. The system prompt ends with a
`Current time:` line, and by default it stays there: Anthropic direct gets one
`cache_control` breakpoint just before it, and every other route caches nothing
past the first message.

`ROBOTHOR_CACHE_LAYOUT_V2=1` moves that line, and the per-run task-context
record (run id, request, objective), out of the system prompt into an `[engine]`
turn just before the latest user message, never into the operator's own text.
The session keeps the record in its own system message, where compaction and
its readers expect it; only the request sent to the provider changes. The system prompt (guardrail summary included), the tool list and the
earlier conversation then stay byte-identical between turns. What each route
gets on top:

| Route | Caching | What the engine sends |
|---|---|---|
| Anthropic direct (`anthropic/…`) | explicit breakpoints | `cache_control` on the last tool, the system prompt, and the last user or tool message before the final two turns (three of the four allowed) |
| Other Claude routes the library marks cacheable (`vertex_ai/claude…`, `bedrock/…`) | explicit | the system-prompt breakpoint only, as before |
| OpenAI (`openai/…`, `azure/…`, bare `gpt-…`) | automatic for prefixes of 1024+ tokens | no `cache_control` |
| Gemini (`gemini/…`, `vertex_ai/gemini…`) | implicit | no `cache_control` (a marker would request explicit context caching, which Gemini refuses on agent calls) |
| OpenRouter (`openrouter/…`) | whatever the upstream does | no `cache_control`, as before |

The time turn is sent as role `developer`; on Anthropic and Gemini it is
rewritten to a user turn like every other engine turn. The cache-hit rate is in
the run's `cache_read_tokens` and the emitted cache-hit-rate metric; compare it
before and after turning the flag on.

## Where models are chosen

| Surface | Source |
|---|---|
| Telegram `/model` keyboard | `AVAILABLE_MODELS` in `robothor/engine/telegram_handlers.py` (checked against the registry by tests; deprecated ids are never listed) |
| New-instance wizard default | `openrouter/xiaomi/mimo-v2.5` |
| Benchmark judge default | `openrouter/xiaomi/mimo-v2.5-pro` (every shipped suite pins its judge explicitly) |
| Fleet default | `docs/agents/_defaults.yaml` on the instance |

Changing the fleet default is decided by a matched benchmark through the real
tools (`docs/benchmarks/`), never by a single run: run-to-run noise on this
harness has been measured at ±9 points.
