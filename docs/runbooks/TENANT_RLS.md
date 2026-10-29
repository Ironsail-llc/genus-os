# Tenant Isolation (Row-Level Security)

## The problem

Tenant isolation rests entirely on the application remembering a `WHERE` clause.

- `robothor/crm/dal.py` remembers: **87** tenant predicates.
- `crm/bridge/crm_dal.py` — the HTTP API's data layer, 2,071 lines — **does not: zero.**
  Its read helpers take a bare id and no tenant (`get_company(company_id)`), so the
  API can return any tenant's row by id.

Rewriting the bridge DAL is one answer. Making Postgres refuse to serve the row is
the durable one: then a forgotten `WHERE` leaks nothing.

## The trap

**A Postgres superuser bypasses RLS unconditionally** — `ENABLE` and `FORCE` are
both ignored for them. The engine connects as `philip`, **which is a superuser**.
So "enabling RLS" alone would have protected nothing at all. Verified:

```
as philip (superuser),   app.tenant_id='acme-corp'  -> sees ALL 3 tenants' rows
as robothor_app (not),   app.tenant_id='acme-corp'  -> sees ONLY acme-corp's row
```

That is why migration 082 exists.

## What ships

| Migration | Effect |
|---|---|
| `081_tenant_rls_backstop.sql` | `ENABLE` + `FORCE` RLS and a `tenant_isolation` policy on all 59 tenant-scoped tables. **Permissive when `app.tenant_id` is unset** — migrations, `psql`, the CLI and every not-yet-tenant-aware path keep working. |
| `082_tenant_rls_app_role.sql` | Creates `robothor_app` (`NOSUPERUSER NOBYPASSRLS`) with the grants the engine needs. Created, **not adopted** — switching the connection user inside a schema migration could lock the instance out of its own database. |

`robothor/db/connection.py` binds `app.tenant_id` on each connection when
`ROBOTHOR_RLS_ENABLED=1`. Off by default, so this is a no-op until you turn it on.

## Turning it on

```bash
# 1. Apply the migrations (081, 082).
# 2. Point the engine at the non-superuser role:
#    /etc/robothor/robothor.env
ROBOTHOR_DB_USER=robothor_app
ROBOTHOR_RLS_ENABLED=1

# 3. Restart, then PROVE it — do not trust the flag:
sudo systemctl restart robothor-engine
psql -U robothor_app -d robothor_memory -c \
  "SELECT set_config('app.tenant_id','robothor-primary',false);
   SELECT DISTINCT tenant_id FROM crm_tasks;"
# must return exactly one tenant. If it returns several, the connection is
# still a superuser and you have no isolation.
```

Rollback: unset `ROBOTHOR_RLS_ENABLED` (policy goes permissive again) and/or point
`ROBOTHOR_DB_USER` back. The policy itself is safe to leave in place.

## No new secret is needed

The services connect over the **Unix socket with peer auth** (`ROBOTHOR_DB_HOST=/var/run/postgresql`),
so `robothor_app` needs no password — map the OS user to the role instead:

```
# pg_ident.conf
robothor_map    <os-user>       robothor_app

# pg_hba.conf — MUST come before the generic `local all all peer` line
local   all   robothor_app   peer map=robothor_map
```

`systemctl reload postgresql`, then `psql -U robothor_app -d robothor_memory` should
connect with no prompt. This avoids provisioning a DB password through SOPS.

## Status: enabled 2026-07-14

Rehearsed first on a scratch database restored from the nightly dump — never on
production. As `robothor_app`, scoped to `robothor-primary`: reads returned exactly
one tenant, writes to the *own* tenant succeeded, and a write to another tenant was
refused (`new row violates row-level security policy`). Every table was readable and
every sequence usable, so migration 082's grants are sufficient. The app uses no
`TRUNCATE` and no runtime DDL, which 082 does **not** grant.

Live on: **engine, bridge, delphi-engine** (`ROBOTHOR_DB_USER=robothor_app`).

## What broke, and why (2026-07-14)

**`delphi-smart-money` issues `CREATE TABLE IF NOT EXISTS` at runtime.** Against the
non-superuser role that fails with `permission denied for schema public`, and
**pre-creating the table does not help** — Postgres checks the CREATE privilege on the
schema *before* it checks whether the table exists, so `IF NOT EXISTS` still raises.

Worked around with `GRANT CREATE ON SCHEMA public TO robothor_app`. Tenant isolation is
unaffected (verified after the grant: one tenant visible, cross-tenant write still
refused) — but the app role now holds more than it needs.

Proper fix: move that DDL into `crm/migrations/`, then
`REVOKE CREATE ON SCHEMA public FROM robothor_app`.

**Lesson for the next cutover:** grep for runtime DDL across *every* service that reads
`ROBOTHOR_DB_USER`, not just `robothor/` and `crm/`. The pre-flight missed delphi's own
code, and the failure only surfaced because `systemctl is-system-running` went
`degraded`.

## Every DB-touching service is now scoped (2026-07-14)

`robothor-orchestrator` and `robothor-vision` **bypassed RLS for their entire
existence**, and nothing said so. Two independent reasons, stacked:

1. Their units never loaded `/etc/robothor/robothor.env`, so they had neither
   `ROBOTHOR_DB_USER` nor `ROBOTHOR_RLS_ENABLED`.
2. `robothor/config.py` then falls back to `$USER` — i.e. `philip`, **a
   superuser**, which bypasses row-level security unconditionally. And with
   `app.tenant_id` never set, migration 081's policy goes *permissive* as well.

So the instance reported RLS "enabled" while two services read every tenant's rows.

Fixed by giving them the main config. Verified: **zero superuser app backends**
(`SELECT usename, count(*) FROM pg_stat_activity WHERE application_name = ''`),
both services healthy, isolation query returns one tenant.

`robothor-app` (the dashboard) is **not** affected — it has no `pg` dependency at
all and reaches data through the bridge/engine API. Its `PG_*` env vars are dead
config.

## The guardrail that would have caught it

`_apply_tenant_scope` now checks `rolsuper` once per process and logs a loud
`RLS IS INERT` error when `ROBOTHOR_RLS_ENABLED` is set but the connection is a
superuser. It is never fatal — it cannot take the instance down, only stop it
lying about its isolation.

**Grep for this in the logs before believing any RLS claim:**

```bash
journalctl -u robothor-engine -u robothor-bridge -u robothor-orchestrator \
           -u robothor-vision -u robothor-delphi-engine | grep "RLS IS INERT"
```

## Per-request binding in the bridge

The bridge binds every **authenticated** request to its token's tenant.
`TenantScopeMiddleware` (`crm/bridge/middleware.py`) runs directly inside
`AuthMiddleware` and wraps the handler in `tenant_scope(auth.tenant_id)`, so
every pooled connection checked out for that request sets `app.tenant_id` to
the token's `tid` — sync handlers included, because anyio copies the context
into the threadpool. A route that forgets its tenant `WHERE` then gets only
that tenant's rows from the database. Unauthenticated requests get no scope.
With `ROBOTHOR_RLS_ENABLED` unset nothing is bound at all, as before.

The vault follows the same scope: `robothor.vault` defaults `tenant_id` to the
enclosing `tenant_scope` (the default tenant outside one), and its connection
is bound like a pooled one. The session signing key is always resolved in the
default tenant, whatever scope asks for it.

**Sign-in is the exception.** A login has no token yet, and the account may
live in any tenant — `(idp_issuer, idp_subject)` is unique platform-wide. The
`/api/auth` sso, login, refresh, logout and switch-tenant routes and
`accounts.get_account_by_idp` / `jit_provision` / `consume_active_session`
therefore run inside `unbound_scope()`, which binds `app.tenant_id = ''` (the
permissive branch of the policy). Without it, an SSO sign-in for an account
outside the default tenant misses the existing row, inserts a duplicate and
fails with a `UniqueViolation`. The caller list is pinned by
`tests/test_unbound_scope_callers.py`; see the shared docstring on
`unbound_scope` and `read_every_tenant_in_transaction` in
`robothor/db/connection.py` for how the two widenings differ.

### Tenant switching

A root-tenant owner or admin may act in a descendant tenant:
`POST /api/auth/switch-tenant {"tenant": "<id>"}` returns an access token with
`tid` set to the target, `home_tid` set to the home tenant and `act: true`. The
acting tenant is carried in the token, never in a header — an `X-Tenant-Id`
that disagrees with `tid` is still refused. The target must be one of the
HOME tenant's descendants (depth 3), so a child admin can reach neither its
parent nor a sibling, and switching from an acting token is checked against
home. `GET /api/tenants/accessible` lists the choices, home first. The refresh
token is not touched: a refresh always returns a home token.

`GENUS_TENANT_SWITCH_MODE` is `off` by default (every switch is refused);
`observe` logs `tenant.switch would-allow` / `would-deny` / `would-refuse` and
still refuses; `enforce` issues the token — **only while row-level security is
genuinely enforcing**. That check is the same gate federation activation uses
(`robothor/federation/rls_gate.py`): it refuses with 503 when
`ROBOTHOR_RLS_ENABLED` is unset or the connection is a superuser.
`GENUS_TENANT_SWITCH_ALLOW_INERT_RLS=1` is the deliberate escape hatch, and is
logged at WARNING every time it admits a switch. Promote the ladder as in
[Guardrail flips](GUARDRAIL_FLIPS.md).
