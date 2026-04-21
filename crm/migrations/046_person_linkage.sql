-- ─────────────────────────────────────────────────────────────────────────────
-- 046_person_linkage.sql
--
-- Phase 1a of the Unified Person Graph (CRM Data Fabric).
--
-- Adds nullable person_id FKs to tables that already capture per-person
-- signal but linked the person only implicitly (string-matched session_key,
-- trigger_detail strings, entity-name TEXT[], chat_id text). After this
-- migration every such row can be queried by FK from the contact's record.
--
-- Tables touched:
--   - chat_sessions          (was: session_key text only — backfill via contact_identifiers)
--   - agent_runs             (was: trigger_detail TEXT — backfill from "chat:<id>" pattern)
--   - channel_message_map    (was: chat_id only — backfill via contact_identifiers)
--   - memory_facts           (was: entities TEXT[] of names — conservative backfill via memory_entity_id)
--
-- Backfills are idempotent; re-running the migration after new data lands
-- will not double-stamp (UPDATE ... WHERE person_id IS NULL).
--
-- Rollback:
--   ALTER TABLE chat_sessions       DROP COLUMN IF EXISTS person_id;
--   ALTER TABLE agent_runs          DROP COLUMN IF EXISTS person_id;
--   ALTER TABLE channel_message_map DROP COLUMN IF EXISTS person_id;
--   ALTER TABLE memory_facts        DROP COLUMN IF EXISTS person_id;
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── chat_sessions.person_id ──────────────────────────────────────────────────
-- session_key formats observed:
--   'telegram:<chat_id>'       — direct telegram chat
--   'agent:main:primary'       — canonical operator session (resolves to owner)
--   '<freeform>'               — webchat / other
ALTER TABLE chat_sessions
    ADD COLUMN IF NOT EXISTS person_id UUID
    REFERENCES crm_people(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_person
    ON chat_sessions(tenant_id, person_id, last_active_at DESC)
    WHERE person_id IS NOT NULL;

-- Backfill: telegram sessions (filter to live people to avoid stale FKs)
UPDATE chat_sessions cs
   SET person_id = ci.person_id
  FROM contact_identifiers ci
  JOIN crm_people p ON p.id = ci.person_id
 WHERE cs.person_id IS NULL
   AND cs.session_key LIKE 'telegram:%'
   AND ci.channel = 'telegram'
   AND ci.identifier = substring(cs.session_key from 10)
   AND ci.person_id IS NOT NULL
   AND p.deleted_at IS NULL;

-- Backfill: canonical operator session ('agent:main:primary') → owner person
UPDATE chat_sessions cs
   SET person_id = tu.person_id
  FROM tenant_users tu
  JOIN crm_people p ON p.id = tu.person_id
 WHERE cs.person_id IS NULL
   AND cs.session_key = 'agent:main:primary'
   AND tu.tenant_id = cs.tenant_id
   AND tu.role = 'owner'
   AND tu.person_id IS NOT NULL
   AND p.deleted_at IS NULL;


-- ── agent_runs.person_id ─────────────────────────────────────────────────────
-- trigger_detail conventions observed:
--   'chat:<chat_id>|sender:<name>'   — telegram-triggered runs
--   'chat:<chat_id>'                  — telegram-triggered runs (no sender)
--   '<other>'                         — cron/hook/event/manual
ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS person_id UUID
    REFERENCES crm_people(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_agent_runs_person
    ON agent_runs(tenant_id, person_id, created_at DESC)
    WHERE person_id IS NOT NULL;

-- Backfill: telegram-trigger runs whose trigger_detail begins with 'chat:<id>'
UPDATE agent_runs ar
   SET person_id = ci.person_id
  FROM contact_identifiers ci
  JOIN crm_people p ON p.id = ci.person_id
 WHERE ar.person_id IS NULL
   AND ar.trigger_type = 'telegram'
   AND ar.trigger_detail LIKE 'chat:%'
   AND ci.channel = 'telegram'
   AND ci.identifier = split_part(split_part(ar.trigger_detail, '|', 1), ':', 2)
   AND ci.person_id IS NOT NULL
   AND p.deleted_at IS NULL;


-- ── channel_message_map.person_id ────────────────────────────────────────────
-- Every map row already has chat_id (the platform identifier). Resolve via
-- contact_identifiers so the unified-feed scan can be a single indexed read.
ALTER TABLE channel_message_map
    ADD COLUMN IF NOT EXISTS person_id UUID
    REFERENCES crm_people(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_channel_map_person
    ON channel_message_map(tenant_id, person_id, created_at DESC)
    WHERE person_id IS NOT NULL;

-- Backfill: telegram-channel rows
UPDATE channel_message_map m
   SET person_id = ci.person_id
  FROM contact_identifiers ci
  JOIN crm_people p ON p.id = ci.person_id
 WHERE m.person_id IS NULL
   AND m.channel = 'telegram'
   AND ci.channel = 'telegram'
   AND ci.identifier = m.chat_id
   AND ci.person_id IS NOT NULL
   AND p.deleted_at IS NULL;


-- ── memory_facts.person_id ───────────────────────────────────────────────────
-- Conservative: only stamp when the fact's entities[] array contains a single
-- entity name that has exactly one contact_identifiers row with a person_id.
-- Multi-entity facts and ambiguous matches stay NULL.
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS person_id UUID
    REFERENCES crm_people(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memory_facts_person
    ON memory_facts(tenant_id, person_id, created_at DESC)
    WHERE person_id IS NOT NULL;

-- Backfill: single-entity facts whose entity name maps unambiguously
WITH unambiguous AS (
    SELECT me.name AS entity_name, ci.person_id
      FROM memory_entities me
      JOIN contact_identifiers ci
        ON ci.memory_entity_id = me.id
       AND ci.person_id IS NOT NULL
      JOIN crm_people p
        ON p.id = ci.person_id
       AND p.deleted_at IS NULL
     GROUP BY me.name, ci.person_id
    HAVING COUNT(*) = 1
)
UPDATE memory_facts mf
   SET person_id = u.person_id
  FROM unambiguous u
 WHERE mf.person_id IS NULL
   AND mf.is_active = TRUE
   AND array_length(mf.entities, 1) = 1
   AND mf.entities[1] = u.entity_name;

COMMIT;
