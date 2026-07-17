# Unified Identity Context — Flags, Rollout Order, CLI, Deploy Notes

Operator runbook for the Unified Identity Context work (`robothor/identity/`,
Phases 1–7). Covers the three rollout flags, the load-bearing order they
must be flipped in, the `robothor user` CLI, and deploy notes for this
branch's migrations. See `docs/runbooks/GUARDRAIL_FLIPS.md` for the sibling
guardrail-flag ladder — these three flags are plain env vars (not in
`GOVERNED_FLAGS`/the systemd drop-in), so there is no live/mirror drop-in
file to sync; set them directly in the engine's environment and restart.

## The three flags

All three are single-var `off → observe → enforce` ladders (see
`robothor/engine/feature_flags.py` for the authoritative docstrings).
`off` is always byte-identical to pre-Unified-Identity-Context behavior.
`observe` never changes output/authorization — it only logs what `enforce`
would have done, for soak review — with one deliberate exception:
`ROBOTHOR_TELEGRAM_ROLE_GATES=observe` fabricates an unregistered GROUP-chat
sender as `role=guest` (zero tool grants, fail-closed) instead of `off`'s
`role=user`, the same value `enforce` uses, so observe soak already reflects
the real deny-all outcome for that path (`telegram.py::_resolve_user`,
`~telegram.py:2688`). `enforce` is the real behavior change everywhere else.

| Flag | Off (default) | Observe | Enforce |
|------|----------------|---------|---------|
| `ROBOTHOR_PER_USER_SESSIONS` | Every webchat caller gets the requested `chat_sessions` key unchanged — one shared session (`agent:main:primary`) for everyone. | Requested key still honored; logs what the derived per-user key *would* have been for non-owner/non-service callers. | Member callers are isolated onto a server-derived `agent:{agent_id}:user:{auth.user_id}` session; the tenant owner keeps `agent:main:primary` (preserves webchat↔Telegram continuity). Applied to send/history/inject/abort/clear/export/plan/deep — also closes the session-ownership hole (any same-tenant caller could previously read any `session_key`). |
| `ROBOTHOR_TELEGRAM_ROLE_GATES` | Owner-only Telegram surfaces (`/restart`, `/agents`, `/steer`, `perm:`/`dp:`/`runctl:` callbacks) check `chat_id == default_chat_id` only — a non-owner posting from the operator's own chat_id passes. Unregistered senders in the primary chat are fabricated as `role=owner` with no DB row. Unregistered group senders are fabricated as `role=user`. | Both the legacy chat_id check and the new per-sender role check run; the OLD chat_id check still gates, but every divergence (and every owner fabrication) is logged loudly so the operator can audit what enforce would decide. | Role check only — chat_id is irrelevant to authorization. Owner fabrication requires an actual `tenant_users` row (no more free ride from `default_chat_id`) unless `ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK=1` (fresh-install escape hatch — see below). Unregistered group senders fabricate as `role=guest` (zero tool grants — `role_permissions` is fail-closed). |
| `ROBOTHOR_DATA_SCOPING` | Every data-read tool (and the warmup prompt-assembly path — see the "warmup scoping" fix below) queries unrestricted, identical to pre-flag behavior. | Queries still unrestricted, but every restricted caller's read logs how many rows the "own data + shared" rule *would* have dropped (`robothor.identity.scope.log_would_drop`). | Non-privileged identities (role not in `{owner, admin, service}`) only see rows where `person_id = their own person_id` or `person_id IS NULL` (org-general). Applies to CRM DAL reads, `memory.facts.search_facts`, and warmup's memory-block/entity-context prompt assembly. |

### Load-bearing rollout order

**`ROBOTHOR_TELEGRAM_ROLE_GATES` must reach `enforce` before
`ROBOTHOR_DATA_SCOPING` reaches `enforce`.**

Why: `robothor.identity.scope.scope_for` treats any identity whose role is
`owner`/`admin`/`service` as **unrestricted** — no data-scoping filter
applies at all. Under `ROBOTHOR_TELEGRAM_ROLE_GATES=off` (or `observe`),
an unregistered sender posting in the operator's primary Telegram chat is
still fabricated a `role=owner` identity (no DB row required). If
`ROBOTHOR_DATA_SCOPING=enforce` is flipped first, that fabricated-owner
fallback resolves to `role=owner` → `DataScope(restricted=False)` →
**unrestricted** — the exact operator-private data the scoping flag exists
to protect is still fully exposed to anyone who can reach the primary chat
unregistered. Flip `TELEGRAM_ROLE_GATES` to `enforce` (which requires a real
`tenant_users` row for owner status) first, soak it, and only then start the
`DATA_SCOPING` ladder.

`ROBOTHOR_PER_USER_SESSIONS` has no ordering dependency on the other two —
it can be flipped independently.

### Escape hatches

- `ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK=1` — restores the
  `default_chat_id` → fabricated-owner fallback even under
  `ROBOTHOR_TELEGRAM_ROLE_GATES=enforce`. For a brand-new instance with no
  `tenant_users` row yet (the very first boot, before `robothor user add`
  has ever run) — without it, enforce would lock the operator out of their
  own bot before they can register themselves. Document this in instance
  init instructions; unset it once the owner is registered.
- `ROBOTHOR_OPEN_ONBOARDING=1` — restores the pre-Unified-Identity-Context
  behavior where any unknown private Telegram sender can self-provision a
  tenant (`robothor/engine/onboarding.py::create_tenant_with_user`). Default
  is unset (closed allowlist): an unregistered private-chat sender gets a
  polite refusal, and the operator gets a rate-limited notification
  containing the sender's Telegram ID and the exact
  `robothor user add --tenant <tenant> --name "<name>" --telegram-id <id> --role <role>`
  command to run.

## `robothor user` CLI

Closed-allowlist registration — the command line the refusal notification
points the operator at. Full identity linkage in one step (not just an
auth row): `tenant_users` + optional `user_accounts` invite + `crm_people`
row + `contact_identifiers` for every provided channel handle, so the
relationship graph and data scoping work for a new user from day one.

```
robothor user list [--tenant TENANT]

robothor user add --name NAME --role ROLE
    [--tenant TENANT] [--telegram-id ID] [--email EMAIL]
    [--person-id PERSON_ID | --create-person]
# ROLE: owner | admin | member | user | viewer | auditor
# When --email is given, prints the robothor auth grant-binding follow-up
# command needed to actually issue the SSO/webchat invite.

robothor user link --telegram-id ID (--person-id PERSON_ID | --email EMAIL)
    [--tenant TENANT]
# Links a Telegram id to an existing crm_people row (looked up by id or email).

robothor user link-face --label LABEL --person-id PERSON_ID
    [--display-name NAME] [--tenant TENANT]
# Upserts a face_identities row — see Phase 7 / migration 089 below.
```

### Registration flow for a new member

1. Unknown sender messages the bot in a private chat → closed-onboarding
   refusal + operator notification with the exact `robothor user add`
   command (rate-limited so a retrying sender can't flood the operator).
2. Operator runs `robothor user add ...` (add `--email` if the person also
   needs dashboard/webchat access).
3. If `--email` was given, operator runs the printed
   `robothor auth grant-binding` command to complete the SSO invite.
4. Sender messages again — `lookup_user` now resolves them, identity
   threads through normally.

## Deploy notes

- **Migrations 086–089** ship in this branch: `086_user_permissions.sql`,
  `087_role_permission_guardrails.sql`, `088_member_role_read_only.sql`,
  `089_face_identities.sql`. `robothor/migrations/manifest.txt` was also
  backfilled for `081`–`085`, which the runner had never actually applied in
  production (wedged on a checksum mismatch at migration 039 — see the
  2026-07-16 CF Access sign-in incident). **Before deploying this branch,
  check applied-migration status against prod first** (`python -m
  robothor.db.migrate status` reports applied/pending/DRIFT/MISSING per
  migration) — do not assume 081–085 are already live just because they
  predate this branch.
- **Migration 088 is a live behavior change**, not a pure addition: it
  UPDATEs the `role_permissions` row migration 071 seeded
  (`tenant_id='__default__', role='member', tool_pattern='*'`) from `allow`
  to `deny`, tightening the `member` role to read-only (same shape as
  `viewer`: `search_*`/`get_*`/`list_*` allow, everything else deny). Any
  existing `member`-role user loses non-read tool access the moment this
  migration runs — confirm that's expected for the instance being deployed
  before applying.
- `089_face_identities.sql` is additive only (new table, `CREATE TABLE IF
  NOT EXISTS`); no live behavior change until `robothor user link-face` is
  actually run.

## Architecture

See `docs/SYSTEM_ARCHITECTURE.md` → "Cross-System Identity" for the short
architecture summary (CRM person = canonical identity, channels = bindings,
CURRENT USER prompt block, own-data+shared scoping model).

## Known follow-ups (not yet done)

- **Bridge REST `get_person_*` routes are unscoped** (`crm/bridge/routers/people.py`,
  `crm/bridge/crm_dal.py`) — a separate auth surface from the engine tool
  layer that `ROBOTHOR_DATA_SCOPING` covers. Must be scoped before the first
  external (non-owner) member is given bridge REST access in practice.
- **`_iterate_plan` feedback is ungated** (`robothor/engine/telegram.py`) —
  content-injection into a creator-privileged plan is bounded by the
  creator's own role today, but not independently authorization-checked.
- **`robothor/tests/` migration integration tests are not run in CI** — this
  pre-existing gap also covers the 085 and 088 tests; they only run locally
  against `ROBOTHOR_TEST_DB_DSN`.
