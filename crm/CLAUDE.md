# crm/ — CRM Stack

Native PostgreSQL tables, Bridge webhook service, and contact resolution.

## Architecture

- **Bridge** (`bridge/bridge_service.py`) — FastAPI on port 9100. Middleware: tenant isolation, RBAC, correlation IDs.
- **Contact resolution** (`bridge/contact_resolver.py`) — resolves incoming messages to CRM person records.
- **Routers** in `bridge/routers/` — people, conversations, notes/tasks, memory, notifications, agents, tenants, audit.
- **Migrations** in `migrations/` — numbered SQL files applied in order.

## Owner-aware resolution

`resolve_contact()` and `search_people()` know who the operator is via `tenant_users.person_id → crm_people.id` (migration 039, driven by `~/.robothor/owner.yaml`). When a name-only lookup hits the operator's first/last/nickname and no channel identifier disambiguates, the operator row wins — even if other CRM contacts share the first name. See `get_owner_person()` in `robothor/crm/dal.py` and the platform rule in the root `CLAUDE.md`.

## Testing

```bash
pytest crm/tests/
```
