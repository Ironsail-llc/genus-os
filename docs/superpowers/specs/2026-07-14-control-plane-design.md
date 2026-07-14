# The Helm — Guardrail Control Plane (slice 1)

**Date:** 2026-07-14
**Status:** approved
**Slice:** 1 of 3 — control plane. Later: fleet/runs/workflows/health accounting; canvas bridge.

## Why

**Twelve** flags govern what agents may do, and they live **only in systemd
environment variables**. There is no table, no API, no UI. Changing one means
editing a drop-in as root and restarting the engine.

- **8 on the mode ladder** (`off → observe → alert → enforce`): `RBAC_MODE`,
  `INJECTION_SCAN_MODE`, `EXEC_ALLOWLIST_STRICT_MODE`, `APPROVAL_MODE`,
  `SANDBOX_DEFAULT_MODE`, `COMPLETION_CONTRACTS_MODE`, `RIP_7_MODE`, `RIP_13_MODE`
- **4 boolean toggles**: `RIP_1_ENABLED`, `RIP_4_ENABLED`, `RIP_5_ENABLED`,
  `JUDGE_ENABLED`

(`TELEGRAM_*` and `TRAJECTORY_SAMPLE` are also read from env, but they are
credentials and tuning knobs — not guardrails. They stay in env.)

Two of today's production incidents were caused by exactly that dance.

Worse: on 2026-07-13/14 a hardening pass found **eleven controls that were
built, wired, documented, unit-tested — and did nothing.** Three of them were
fixes made earlier the same day. Every one produced a *quiet* success signal: an
empty events table, a `True` return, a passing test.

The dashboard cannot help with any of this, because it is a CRM viewer. Its four
views total 411 lines; `dashboard-view.tsx` is 19 of them.

This builds the wheel.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Control depth | **Full read + write** | An operator needs a helm, not a gauge. |
| Write-path safety | **Agents structurally cannot reach it** | Not "blocked" — *absent*. See below. |
| Slicing | **Control plane first** | It is the only surface with nothing today, and the only one where a design mistake costs security rather than convenience. |
| Canvas power | **Read anything; act only via parent-chrome confirm** | The agent composes the instrument; the operator pulls the trigger. |
| Inert detection | **Build it, loudly** | It would have caught 9 of today's 11 findings unaided. |

## Architecture

### Source of truth

`feature_flags` table becomes authoritative. Resolution order:

```
DB value  →  os.environ  →  coded default
```

Env is demoted to an **emergency override**: if the DB is unreachable, or an
operator must force a value from a shell, env still wins. Today's behaviour
becomes the failsafe rather than being replaced.

### The write path is hostile to agents

Three independent locks, in order of strength:

1. **No tool exists.** `robothor/engine/tools/schemas.py` never gains a control
   tool. An agent cannot call what is not offered. This is structural, not a
   permission check.
2. **It lives on the bridge**, not the engine. Agents talk to the engine.
3. **Operator role required**, via the auth middleware added in #176. An agent's
   service token is not an operator token.

A prompt-injected agent has no path to this API. That was the precondition for
granting the UI write authority at all.

### Propagation

Flags are read on every guardrail check, so a DB round-trip per check is
unacceptable.

- In-process cache, ~5s TTL.
- `pg_notify` on write → every process invalidates.
- A flip reaches the engine in seconds, **with no restart**.

## The inert-control detector

> A control that is "enabled" and has never fired is not protection. It is a lie
> you are relying on.

For each flag, join **mode** × **evidence** × **can-it-fire**, and classify:

| Verdict | Meaning | Real 2026-07-14 example |
|---|---|---|
| `ENFORCING` | enforce + evidence present | `injection_scan` — blocks recorded |
| `INERT` | enforce/observe, **zero evidence ever** | `human_approval` — enforce, but no agent sets `human_approval_tools`, so nothing can *ever* escalate |
| `BLIND` | observe, but writes no events | `exec_allowlist` observe — the aggregator discarded them; "zero events" meant blind, not clean |
| `UNPROVEN` | promoted recently, no traffic yet | flags flipped today |

### The trap it must not fall into

**Each control writes evidence to a different table.** RIP-7 writes to
`memory_facts_audit`, *not* `agent_guardrail_events`. Querying the wrong table
returns a misleading zero — which would make the detector itself a liar.

Therefore evidence sources are **declared per-flag, in code, beside the flag
definition.** Never inferred.

### Honesty constraint

The detector cannot prove a control *works* — only that it has *fired*. So it
never renders "healthy". It renders:

- `last fired: 3h ago (47 events / 7d)`, or
- `NEVER FIRED — this control cannot protect you.`

**Zero events is never green. It is a question.**

## The canvas bridge (shape now, build in slice 3)

The API must be shaped for this now or slice 3 reworks it.

**The iframe gets:** `srcdoc`, `sandbox="allow-scripts"` (**no**
`allow-same-origin`), DOMPurify, `postMessage`.
**It does not get:** a token, cookies, `fetch`, or any network. It cannot reach
the API even if the LLM writes code that tries.

**Reads.** The iframe posts a typed intent — `{op: "get_agent", id: "..."}` —
against a **declared whitelist of read ops**, never a URL. The parent calls the
API with the *operator's* session and posts data back. An op not on the list is
dropped, logged, **and surfaced**: an LLM page reaching for something it was not
given is a signal worth seeing.

**Actions.** The iframe cannot act. It may only *propose*:

```json
{"op": "propose", "action": "set_flag", "args": {...}, "label": "..."}
```

The parent renders that as a button **in parent chrome, outside the iframe** —
which the iframe cannot draw, style, or fake. The confirmation shows the
**parent's** rendering of the action, derived from the parent's data, not the
iframe's claim.

**Why this specific detail matters:** if the confirm were drawn inside the
iframe, an injected page could label a button "Refresh" while proposing
`set_flag injection_scan=off`. The LLM chooses *which* action to offer. It never
chooses what the confirmation *says*.

This is what "bridging what is said and what is at hand" means, safely: the agent
renders your live system and proposes. It never acts.

## Components

**New:**

| Unit | Purpose |
|---|---|
| `crm/migrations/084_feature_flags.sql` | `feature_flags` + `feature_flag_audit`, seeded from the 12 governed env values |
| `robothor/flags/store.py` | DB-first resolution, TTL cache, `pg_notify` invalidation |
| `robothor/flags/evidence.py` | per-flag evidence sources; the INERT/BLIND/ENFORCING verdict |
| `crm/bridge/routers/controls.py` | `GET /api/controls`, `PATCH /api/controls/{flag}` — operator-only, audited |
| `app/src/components/views/controls-view.tsx` | the Controls tab |

**Changed:** `robothor/engine/feature_flags.py` — the readers for the 12
governed flags call `store.resolve(name)` instead of `os.environ.get`. Credential
and tuning reads are left alone. **Signatures unchanged**, so there is no
call-site churn. That is the entire seam.

## Data flow

```
guardrail check → feature_flags.rip_7_mode() → store.resolve()
                    → cache hit (~0µs) | DB read
write → bridge (operator-only) → audit row → pg_notify → all processes invalidate (~5s)
```

## Testing

Written against the failure modes this system has actually exhibited.

- **The fallback chain is pinned.** DB wins; DB down → env; env unset → coded
  default. A store that silently returned `off` when the DB blinked would
  disable *every guardrail at once*. This test makes that impossible.
- **Agent-hostility is pinned by a test, not a comment.** A test asserts
  `schemas.py` exposes **no** tool matching `flag|control|guardrail`. If someone
  later adds one, CI fails.
- **Evidence sources are pinned per-flag.** A test asserts each declared source
  table exists and is queryable — so the detector cannot silently read a missing
  table and report a comforting zero.
- **The inert detector is tested against a known-inert control**, not a mock:
  `human_approval` genuinely has zero events; the test asserts it returns
  `INERT`.

> The thing that detects hollow controls must not itself be a hollow control.

## Out of scope (later slices)

- Fleet / Runs / Workflows / Health tabs (slice 2)
- Canvas bridge implementation (slice 3)
- Tasks and Marketplace views — already exist, unchanged
