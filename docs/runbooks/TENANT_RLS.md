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

## What is NOT yet isolated

`robothor-app` (dashboard), `robothor-orchestrator` and `robothor-vision` use a
different variable set — `PG_USER=philip` over TCP — so they still connect as a
**superuser and bypass RLS**. They are read-mostly, but until they are switched the
isolation is real for the agent-execution path and *not* for the dashboard. Moving
them needs either a `robothor_app` password (they connect to `127.0.0.1`, not the
socket) or a switch to the socket + peer map above.

Do not describe this instance as tenant-isolated end-to-end until that is done.
