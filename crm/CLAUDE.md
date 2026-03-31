# crm/ — CRM Stack

Native PostgreSQL tables, Bridge webhook service, and contact resolution.

## Architecture

- **Bridge** (`bridge/bridge_service.py`) — FastAPI on port 9100. Middleware: tenant isolation, RBAC, correlation IDs.
- **Contact resolution** (`bridge/contact_resolver.py`) — resolves incoming messages to CRM person records.
- **Routers** in `bridge/routers/` — people, conversations, notes/tasks, memory, notifications, agents, tenants, audit.
- **Migrations** in `migrations/` — numbered SQL files applied in order.

## Testing

```bash
pytest crm/tests/
```
