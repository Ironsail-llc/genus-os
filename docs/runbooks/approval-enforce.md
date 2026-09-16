# Runbook: Flipping Human-Approval to Enforce Mode

The human-approval escalation loop lets a guardrail pause a tool call and
require an operator's yes/no via Telegram before it proceeds:

```
guardrail check_pre_execution → action="escalate"
  → PermissionEscalationManager.request_approval (runner.py:1910-1984)
  → Telegram inline-keyboard prompt (permission_escalation.py:_send_prompt)
  → operator taps Approve / Approve All / Deny
  → on_permission_decision callback (telegram.py) → manager.resolve(...)
  → request_approval returns → runner proceeds or denies the tool call
```

This is gated by `ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED` +
`ROBOTHOR_APPROVAL_MODE` (`off` / `observe` / `enforce`), read by
`fail_closed_on_missing_manager()` in
`robothor/engine/permission_escalation.py`. `off` and `observe` auto-approve
when no approver is reachable (legacy behavior — `observe` additionally logs
a `agent_guardrail_events` row so you can see what *would* have blocked).
`enforce` denies the tool call outright when no manager is wired.

## Prerequisites before flipping to `enforce`

1. **The permission manager must actually be wired.** `daemon.py` calls
   `init_permission_manager(bot, config.default_chat_id)` only when a
   Telegram bot token is configured (`ROBOTHOR_TELEGRAM_BOT_TOKEN` /
   `bot_token` non-empty). Confirm the engine log shows `Permission
   escalation manager wired to Telegram` on startup. Without a wired
   manager, `enforce` denies every escalated tool call — with no way for
   the operator to approve it.
2. **At least one agent has `human_approval_*` guardrails opted in** and has
   actually triggered an `escalate` action recently, so the soak below has
   real signal (not silence because nothing ever escalates).
3. **48-hour observe soak, verified working — not just silent.** Run with
   `ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED=1` and
   `ROBOTHOR_APPROVAL_MODE=observe` for at least 48 hours. Use
   `scripts/guardrail_watch.py` (default `GUARDRAIL_WATCH_HOURS=48`) to see
   `agent_guardrail_events` rows tagged `mode=observe`. Zero would-block
   events is necessary but not sufficient — also confirm at least one real
   escalation went through the full loop: a Telegram prompt was delivered
   with working Approve/Approve All/Deny buttons, and tapping one actually
   resolved the run (not just auto-approved by timeout or fallen through a
   silent send failure). See `robothor/engine/tests/test_approval_e2e.py`
   and `test_telegram.py::TestPermissionCallbacks` /
   `TestPermissionEscalationWiring` for the seam this soak is verifying in
   miniature.
4. **Timeout = deny semantics understood by the operator.** Each escalated
   tool call auto-denies after `human_approval_timeout` seconds (per-agent
   config, default 300s) if nobody responds — this is fail-secure, not a
   bug. Under `enforce`, a missed prompt or an unreachable bot means the
   call is denied, not silently allowed.

## Flipping to enforce

```
ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED=1
ROBOTHOR_APPROVAL_MODE=enforce
```

Restart the engine daemon so the env change takes effect. Watch
`scripts/guardrail_watch.py` and application logs for the first 24 hours
after the flip for any `action=blocked` rows on `guardrail_name` values you
did not expect to trip.

## Rollback

Set `ROBOTHOR_APPROVAL_MODE=observe` and restart the daemon. This immediately
reverts to auto-approving escalations when no approver is reachable — the same
legacy behavior as before this feature existed.

Prefer `observe` to unsetting the variables. `off` and `observe` are both
values the Controls page offers and audits; an unset name is a gap nothing
records.

## Both variables, or none of it

`approval_mode()` is `_enforcement_mode("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED",
"ROBOTHOR_APPROVAL_MODE")`, and `_enforcement_mode` returns `off` whenever the
first is falsy **regardless of the second**. Measured:

| Environment | `approval_mode()` |
|---|---|
| unset / unset | `off` |
| `ROBOTHOR_APPROVAL_MODE=enforce` alone | `off` |
| `ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED=1` | `observe` |
| both, with `MODE=enforce` | `enforce` |

The second row is the trap: the mode is the name an operator reaches for, and
on its own it changes nothing. `genus doctor --category agents` reports this
as `agents.approval_gate_not_armed` whenever an agent grants a destructive tool
the running engine will not gate.

## Status 2026-09-16: the gate has its first real user

Superseding the 2026-07-13 note below: `crm-steward` now declares
`v2.guardrails: [human_approval]` and `v2.human_approval_tools:
[delete_person]`, so the "nothing can escalate" condition no longer holds and
promoting the mode is no longer a no-op.

Both variables are set in
`infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf` and, as of
this change, in `helm/genus-os/values.yaml` under `engine.env`. They were
previously in the systemd unit only — so on the Helm/ArgoCD path, which is how
production is actually deployed, the gate was `off` and the manifest's
declaration protected nothing.

### Status 2026-07-13 (historical): the gate was INERT, not clean

A soak audit found **zero escalations had ever occurred** — and the reason was
not that agents behave well. No agent manifest set `human_approval_tools`, so
`runner.py` never called `set_human_approval_patterns()`,
`_human_approval_patterns` stayed empty for every agent, and
`_check_human_approval()` returned an empty result for every tool call.
**Nothing could escalate, so nothing could be approved or denied.**

That was a statement about adoption, not about the mechanism: probed directly,
with the policy enabled and a pattern set, `delete_person` returns
`allowed=False action='escalate'` while `list_people` stays allowed. The
prerequisite it named — "real signal, not silence because nothing ever
escalates" — is what `crm-steward` now supplies.

Before adding the next tool to a `human_approval_tools` list (candidates:
outbound email/SMS, `exec`, payments, calendar writes on external attendees,
destructive CRM mutations), verify one real escalation completes the Telegram
approve/deny round-trip, then soak 48h.

Note that `human_approval_fail_open: true` on an agent defeats `enforce`
entirely, per agent. The doctor check reports that too.
