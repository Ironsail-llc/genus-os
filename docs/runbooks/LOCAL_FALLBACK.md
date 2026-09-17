# Runbook: the local fallback

**What this is for.** Every agent's model chain ends on a local model, so the
fleet keeps answering when no cloud credential works. This runbook says what
happens when that day comes, what the operator sees, and which checks prove the
tier will actually answer before it has to.

## The failure this exists for

On 2026-09-16 the instance's OpenRouter key hit its weekly cap. The fleet fell
back to the local tier, answered twelve steps of a Telegram conversation on it,
and then reported to the operator:

```
Failure: error. All models failed to respond.
```

Three things were wrong, and all three are fixed:

| What went wrong | What happens now |
|-----------------|------------------|
| The compaction threshold came from the configured **primary**, so the conversation grew past the local model's 65,536-token window. A model skipped for a spent credential is not "broken", so the sizing never moved to it. | The budget is the **reachable** model's: `context_fit.next_reachable_model` asks the same three questions the chain walk asks — broken, breaker open, credential pool spent. |
| Ollama answered `no user query found in messages` (its reply when a conversation exceeds `num_ctx`: it truncates from the front, and the tail alone left no user turn). That looked like a 500, so it was retried five times with backoff. | Overflow is its own class: no backoff, the messages are shrunk (tool results dropped oldest-first, protected head and last user turn kept), and the **same** model is re-asked once. |
| The operator read a Python `RuntimeError`. | The run's last word names each model and why it is out, says whether the local tier answered or was unreachable, and — when a credential is spent — what to do about it. Before giving up, the engine makes one last minimal attempt (protected head + the last user turn) against a local model the server actually carries. |

### What Ollama does with a conversation that does not fit

Measured against the local server while this was written, because the two
behaviours have very different symptoms:

* If the conversation still ends on a **user** turn, the server truncates from
  the front and **answers normally**. Nothing errors. The agent simply loses
  the beginning of its own context, silently, and reasons from the hole.
* If the tail that survives truncation holds **no** user turn — a large tool
  result and an engine note after the question, which is what step 13 of any
  busy run looks like — it answers `no user query found in messages`.

The engine now keeps the assembled messages under the model's window, so
neither happens. The second one is the incident; the first is the one nobody
would ever have noticed.

### After deploying this

Run `genus migrate` on the instance **and on any benchmark pod**. Migration
`124_guardrail_context_overflow` adds `context_overflow` to the
`agent_guardrail_events` action CHECK, which is where the new control records
that it acted. Until it is applied the engine writes those rows as `warned`
instead and logs one line naming the migration — the row is never dropped, but
the evidence table under-reports the control until you run it.

## What the operator sees

* A **critical page** the moment the last credential for a provider is retired,
  naming the provider, the reason, the local reset window and the recovery:
  top up or raise the limit, then run `genus secrets reload`.
* Runs keep completing on the local tier, slower. `DEGRADED model: agent=… ran
  on ollama_chat/…` is logged per run, and `Primary model unreached` alerts.
* If even the local tier cannot answer, the failure message is a sentence, not
  a traceback.

## Checks

```bash
genus doctor --only models.local_fallback_ready
```

Recommended, and it runs on any instance whose chain names an `ollama_chat/`
model; it skips on a cloud-only one. Recommended rather than required on
purpose: a local server that is restarting must not take `genus doctor` — and
any install gate or CI job built on it — from exit 0 to exit 1. The failure
names the consequence instead. It fails when the server does not answer, when the model is in
the chain but not on the server (it names the `ollama pull`), when the
registry's window is larger than the model's own context (the engine sends that
number as `num_ctx`, so a larger one is silently trimmed), or when compaction
would fire too late to leave room for the answer.

```bash
genus doctor --only models.local_fallback_probe --timeout 120
```

Opt-in positive control: it pushes a conversation 1.5x the model's window
through the engine's own call path and requires an answer. It never runs in a
default sweep — it spends a real generation — and `--only` is the consent.

## When the cloud key is exhausted

1. The page names the provider. Top up, or raise the limit at the provider.
2. Run `genus secrets reload`. **This is not optional.** The engine holds a
   quota-retired key out for six hours in its own memory and cannot see that a
   cap moved; the reload is what puts it back in rotation, without a restart.
3. `genus secrets status` shows each credential's live pool state — in
   rotation, or retired with the reason and how long is left. Fingerprints
   only, never a value.

## Related

* [Configuration → credential pool](../configuration.md)
* `docs/runbooks/PAGING.md` in the repository — where the exhaustion page goes
* `docs/runbooks/INSTANCE_DOCTOR.md` in the repository — the check catalogue
