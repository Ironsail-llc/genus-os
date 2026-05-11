-- 052_multi_tenant_telegram_users.sql
--
-- Allow one Telegram user to belong to multiple tenants (e.g., the operator
-- running both the primary Robothor persona and a second persona like Delphi).
-- The original constraint on tenant_users from 033 enforced a single tenant
-- per Telegram user, which blocks multi-persona deployments where the same
-- human wears different hats on different bots.
--
-- New invariant: (telegram_user_id, tenant_id) is unique. A single human
-- appears at most once per tenant. Code must pass tenant context into
-- lookup_user() so per-tenant rows resolve correctly.

BEGIN;

ALTER TABLE tenant_users DROP CONSTRAINT IF EXISTS tenant_users_telegram_user_id_key;

ALTER TABLE tenant_users
    ADD CONSTRAINT tenant_users_telegram_tenant_key
    UNIQUE (telegram_user_id, tenant_id);

COMMIT;
