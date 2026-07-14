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

## Why it isn't enabled yet

Switching the engine's database user on a live single-box instance is the kind of
change that takes production down if a grant is missing. The migrations, the
connection support, and the proof are all in place; flipping it deserves a
maintenance window and the verification query above — not a drive-by.
