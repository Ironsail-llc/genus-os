-- Migration 098: tenant_id column DEFAULTs stop naming the first instance.
--
-- Migrations 033 (memory multi-tenancy), 063 (benchmark_results), 075
-- (operator signals), and infra/031 (agent_reviews) shipped
-- ``DEFAULT 'robothor-primary'`` on tenant_id columns — the first instance's
-- tenant id baked into platform schema (CLAUDE.md rule 1).  On any other
-- install, a code path that omits tenant_id silently writes rows into a
-- tenant named after somebody else's instance.
--
-- This migration retargets those DEFAULTs to 'default', the tenant every
-- install seeds (crm_tenants row created in 008_multi_tenancy / infra 001).
-- Data is untouched: only the column DEFAULT changes, and every production
-- writer of the retargeted tables passes tenant_id explicitly (audited
-- 2026-08-20 across robothor/ and crm/), so no row changes tenant anywhere.
--
-- DELIBERATELY KEPT UNCHANGED — writers still rely on the column DEFAULT,
-- so retargeting would silently move their new rows between tenants on the
-- first instance:
--   * benchmark_results.tenant_id  — robothor/engine/tools/handlers/
--     benchmark.py inserts without tenant_id.
--   * memory_insights.tenant_id    — robothor/memory/lifecycle.py inserts
--     without tenant_id.
-- Retarget these in a follow-up only after those writers pass tenant_id
-- explicitly.
--
-- Note: an instance whose DB carries additional locally-ALTERed tenant_id
-- DEFAULTs (outside these platform migrations) must normalize those locally;
-- platform migrations only govern platform-shipped schema.

BEGIN;

-- From 033_memory_multi_tenancy.sql (memory_insights deliberately excluded).
ALTER TABLE memory_facts        ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE memory_entities     ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE memory_relations    ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE agent_memory_blocks ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE contact_identifiers ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE ingested_items      ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE ingestion_watermarks ALTER COLUMN tenant_id SET DEFAULT 'default';

-- From 075_operator_signals.sql.
ALTER TABLE message_reactions   ALTER COLUMN tenant_id SET DEFAULT 'default';
ALTER TABLE run_interventions   ALTER COLUMN tenant_id SET DEFAULT 'default';

-- From infra/031_agent_reviews.sql.
ALTER TABLE agent_reviews       ALTER COLUMN tenant_id SET DEFAULT 'default';

COMMIT;
