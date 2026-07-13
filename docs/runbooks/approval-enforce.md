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

Set `ROBOTHOR_APPROVAL_MODE=observe` (or unset both env vars to fully
disable the gate) and restart the daemon. This immediately reverts to
auto-approving escalations when no approver is reachable — the same
legacy behavior as before this feature existed.

## Status 2026-07-13: the gate is INERT, not clean

A soak audit found **zero escalations have ever occurred** — and the reason is
not that agents behave well. No agent manifest sets `human_approval_tools`, so
`runner.py` never calls `set_human_approval_patterns()`, `_human_approval_patterns`
stays empty for every agent, and `_check_human_approval()` returns an empty
result for every tool call. **Nothing can escalate, so nothing can be approved
or denied.**

Consequences:
- `ROBOTHOR_APPROVAL_MODE=enforce` today would be a **no-op** — zero blast
  radius, but also zero protection. Promoting it would be security theater.
- Prerequisite 2 above ("real signal, not silence because nothing ever
  escalates") is the binding one, and it is unmet.

To make the gate real, decide which tools genuinely warrant a human in the
loop (candidates: outbound email/SMS, `exec`, payments, calendar writes on
external attendees, destructive CRM mutations), add them to the relevant
agents' `human_approval_tools`, verify one real escalation completes the
Telegram approve/deny round-trip, then soak 48h and promote.
