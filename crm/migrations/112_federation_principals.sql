-- 112: give a federation connection a TENANT and a PRINCIPAL, and put the
-- federation tables under RLS.
--
-- Before this, an inbound federation op executed with no identity at all:
-- federation_responder._trigger called runner.execute() with no user_id, no
-- user_role and no tenant_id. TriggerType.FEDERATION is a system trigger, so
-- runner.py fell back to `agent_config.service_role or "service"` -- and
-- migration 107 seeds service -> ('*','allow'). A peer holding one capability
-- got allow-all tool access, unlogged as federation. _list_runs was raw SQL
-- with no tenant predicate, returning every tenant's runs.
--
-- The fix reuses the authorization machinery this platform already has rather
-- than inventing a second one: role_permissions for the role, user_permissions
-- (migration 086) for per-connection tightening, and tenant RLS for scoping.

BEGIN;

-- ── The connection carries who the peer is, locally ──────────────────
ALTER TABLE federation_connections
    ADD COLUMN IF NOT EXISTS tenant_id            TEXT NOT NULL DEFAULT 'default',
    -- inbound  = they dialled us (we are the hub / parent)
    -- outbound = we dialled them (we are the child)
    ADD COLUMN IF NOT EXISTS direction            TEXT NOT NULL DEFAULT 'outbound',
    -- The principal this peer acts as ON THIS INSTANCE. Enforcement never
    -- requires a user_accounts row: role and user-permission lookups fail
    -- CLOSED, whereas a missing account row would fail OPEN.
    ADD COLUMN IF NOT EXISTS local_principal_id   TEXT,
    ADD COLUMN IF NOT EXISTS local_principal_role TEXT,
    ADD COLUMN IF NOT EXISTS transport            JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_seen_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error           TEXT,
    ADD COLUMN IF NOT EXISTS activated_at         TIMESTAMPTZ;

ALTER TABLE federation_connections
    DROP CONSTRAINT IF EXISTS federation_connections_direction_check;
ALTER TABLE federation_connections
    ADD CONSTRAINT federation_connections_direction_check
    CHECK (direction IN ('inbound', 'outbound'));

ALTER TABLE federation_events
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

-- ── RLS. Migration 081 skipped these three because they had no tenant_id ──
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['federation_connections', 'federation_events']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (
                current_setting('app.tenant_id', true) IS NULL
                OR current_setting('app.tenant_id', true) = ''
                OR tenant_id = current_setting('app.tenant_id', true)
            )
            WITH CHECK (
                current_setting('app.tenant_id', true) IS NULL
                OR current_setting('app.tenant_id', true) = ''
                OR tenant_id = current_setting('app.tenant_id', true)
            )$f$, t);
    END LOOP;
END $$;

-- ── The two federation roles ─────────────────────────────────────────
-- MUST be seeded here. check_tool_permission fails closed on a role with no
-- rows, so an unseeded role would deny everything -- which is safe, but would
-- make a correctly-granted parent silently useless and send the next operator
-- looking for a transport bug. Migration 107 is the precedent.
--
-- federation_child is DENY-ALL. That is what makes "a child has no control
-- over its parent" the default rather than a checkbox someone must remember.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'federation_child',  '*',          'deny'),
    ('__default__', 'federation_parent', '*',          'deny'),
    ('__default__', 'federation_parent', 'get_*',      'allow'),
    ('__default__', 'federation_parent', 'list_*',     'allow'),
    ('__default__', 'federation_parent', 'search_*',   'allow')
ON CONFLICT DO NOTHING;

COMMIT;
