-- Migration 176: memory graph foundation — mentions, edge evidence, time,
-- origin and scope.
--
-- WHY
--   A fact names its entities only as strings (memory_facts.entities TEXT[]),
--   an edge carries one fact_id and no time or lifecycle, and an entity is
--   identified by its exact (tenant, name, type). This adds the schema the
--   graph needs to be the associative backbone of memory:
--
--   * memory_resolution_decisions — one row per decision that a surface form
--     refers to an entity (or was left unresolved), with method, confidence
--     and evidence. A merge never rewrites ids; reversing a decision sets
--     reversed_at and, when replaced, superseded_by.
--   * memory_mentions — the fact <-> entity join, by id, through a decision.
--   * memory_relation_evidence — every fact supporting an edge, not just one.
--   * memory_relations — canonical predicate, event time (valid_from/valid_to),
--     recorded time (recorded_at/expired_at), is_active with retired_reason,
--     origin, and a learned weight.
--   * memory_facts — origin, event_time (when it happened; valid_from is when
--     it was recorded as true) and scope.
--   * memory_entities — its CRM person or company, an embedding, and scope.
--
--   Origin, scope, retirement-reason and method values are the constants in
--   robothor/memory/graph_schema.py; a test compares them to the CHECKs here.
--
-- SAFETY
--   Additive only: every new column is nullable or has a constant default, so
--   code written before this migration reads and writes as before. The content
--   hash (fact_text|tenant_id|category|person_id) covers none of the new
--   columns, so no stored hash changes.
--
--   Locks. The runner applies this file in one transaction, so each lock is
--   held until it commits. Statements are ordered so the strongest lock on
--   memory_facts (ACCESS EXCLUSIVE, from ADD COLUMN) is taken last and held
--   only for the few metadata-only statements after it. No index is built on
--   memory_facts, and its CHECKs are added NOT VALID: every existing row holds
--   NULL or the default, so there is nothing to scan for.
--
-- IDEMPOTENT: IF NOT EXISTS everywhere; constraints are added only when absent;
-- the evidence backfill is ON CONFLICT DO NOTHING. Safe to run again, and safe
-- to re-run under a different number.
--
-- Rollback:
--   DROP TABLE IF EXISTS memory_mentions, memory_relation_evidence,
--       memory_resolution_decisions;
--   ALTER TABLE memory_relations DROP COLUMN IF EXISTS predicate, DROP COLUMN IF EXISTS valid_from,
--       DROP COLUMN IF EXISTS valid_to, DROP COLUMN IF EXISTS recorded_at,
--       DROP COLUMN IF EXISTS expired_at, DROP COLUMN IF EXISTS is_active,
--       DROP COLUMN IF EXISTS retired_reason, DROP COLUMN IF EXISTS origin,
--       DROP COLUMN IF EXISTS weight;
--   ALTER TABLE memory_entities DROP COLUMN IF EXISTS crm_person_id,
--       DROP COLUMN IF EXISTS crm_company_id, DROP COLUMN IF EXISTS embedding,
--       DROP COLUMN IF EXISTS scope_kind, DROP COLUMN IF EXISTS scope_id;
--   ALTER TABLE memory_facts DROP COLUMN IF EXISTS origin, DROP COLUMN IF EXISTS event_time,
--       DROP COLUMN IF EXISTS scope_kind, DROP COLUMN IF EXISTS scope_id;

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. New tables. Empty, so their foreign keys validate instantly; creating
--    them takes SHARE ROW EXCLUSIVE on memory_facts, memory_entities and
--    memory_relations (reads continue, writes wait for the commit).
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS memory_resolution_decisions (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL REFERENCES crm_tenants(id),
    surface_form       TEXT NOT NULL,
    entity_type        TEXT,
    -- NULL: the surface form was deliberately left unresolved.
    resolved_entity_id INTEGER REFERENCES memory_entities(id) ON DELETE SET NULL,
    method             TEXT NOT NULL
        CONSTRAINT memory_resolution_decisions_method_check
        CHECK (method IN ('strong_key', 'full_name', 'context', 'llm', 'exact', 'operator',
                          'backfill')),
    confidence         DOUBLE PRECISION
        CONSTRAINT memory_resolution_decisions_confidence_check
        CHECK (confidence >= 0 AND confidence <= 1),
    evidence           JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed_at        TIMESTAMPTZ,
    superseded_by      BIGINT REFERENCES memory_resolution_decisions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_resolution_decisions_surface
    ON memory_resolution_decisions (tenant_id, lower(surface_form));
CREATE INDEX IF NOT EXISTS idx_memory_resolution_decisions_entity
    ON memory_resolution_decisions (resolved_entity_id)
    WHERE resolved_entity_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_mentions (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES crm_tenants(id),
    fact_id      INTEGER NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
    -- NULL: unresolved, or its entity was deleted.
    entity_id    INTEGER REFERENCES memory_entities(id) ON DELETE SET NULL,
    surface_form TEXT NOT NULL,
    entity_type  TEXT,
    decision_id  BIGINT REFERENCES memory_resolution_decisions(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_mentions_tenant_entity
    ON memory_mentions (tenant_id, entity_id);
CREATE INDEX IF NOT EXISTS idx_memory_mentions_fact
    ON memory_mentions (fact_id);
CREATE INDEX IF NOT EXISTS idx_memory_mentions_decision
    ON memory_mentions (decision_id)
    WHERE decision_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_relation_evidence (
    tenant_id   TEXT NOT NULL REFERENCES crm_tenants(id),
    relation_id INTEGER NOT NULL REFERENCES memory_relations(id) ON DELETE CASCADE,
    fact_id     INTEGER NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (relation_id, fact_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_relation_evidence_fact
    ON memory_relation_evidence (fact_id);

-- Migration 120 policied the tables that existed then; a new tenant table
-- carries its own policy or it is readable across tenants.
ALTER TABLE memory_resolution_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_resolution_decisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memory_resolution_decisions;
CREATE POLICY tenant_isolation ON memory_resolution_decisions
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE memory_mentions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_mentions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memory_mentions;
CREATE POLICY tenant_isolation ON memory_mentions
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE memory_relation_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_relation_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON memory_relation_evidence;
CREATE POLICY tenant_isolation ON memory_relation_evidence
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- ═══════════════════════════════════════════════════════════════════════════
-- 2. Each edge's existing fact becomes its first piece of evidence. One
--    set-based pass over memory_relations, before memory_relations is altered
--    (so it runs under the SHARE ROW EXCLUSIVE taken above: edges stay
--    readable while it runs).
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id, created_at)
SELECT r.tenant_id, r.id, r.fact_id, COALESCE(r.created_at, now())
  FROM memory_relations r
 WHERE r.fact_id IS NOT NULL
ON CONFLICT (relation_id, fact_id) DO NOTHING;

-- ═══════════════════════════════════════════════════════════════════════════
-- 3. memory_relations: time, lifecycle, origin, predicate, weight.
--    relation_type stays the phrase as extracted; predicate is its canonical
--    form. valid_from/valid_to: when the relation held in the world.
--    recorded_at/expired_at: when the system started and stopped believing it.
--    recorded_at is NULL on every edge written before this migration (read
--    it as created_at); new rows get now(). Adding it with DEFAULT now()
--    would stamp every existing edge with the migration's time instead.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_relations
    ADD COLUMN IF NOT EXISTS predicate      TEXT,
    ADD COLUMN IF NOT EXISTS valid_from     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_to       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recorded_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS expired_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS retired_reason TEXT,
    ADD COLUMN IF NOT EXISTS origin         TEXT,
    ADD COLUMN IF NOT EXISTS weight         DOUBLE PRECISION NOT NULL DEFAULT 1.0;

ALTER TABLE memory_relations ALTER COLUMN recorded_at SET DEFAULT now();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_relations'::regclass
                     AND conname = 'memory_relations_retired_reason_check') THEN
        ALTER TABLE memory_relations ADD CONSTRAINT memory_relations_retired_reason_check
            CHECK (retired_reason IN ('ended', 'refuted', 'evidence_merged')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_relations'::regclass
                     AND conname = 'memory_relations_origin_check') THEN
        ALTER TABLE memory_relations ADD CONSTRAINT memory_relations_origin_check
            CHECK (origin IN ('operator_confirmed', 'user_stated', 'crm', 'tool',
                              'agent_generated', 'external', 'inferred')) NOT VALID;
    END IF;
END $$;

-- Neighbourhood expansion: the active edges out of, and into, an entity.
-- (The existing UNIQUE (tenant_id, source_entity_id, target_entity_id,
-- relation_type) already serves a lookup by tenant.)
CREATE INDEX IF NOT EXISTS idx_memory_relations_active_source
    ON memory_relations (tenant_id, source_entity_id)
    WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_memory_relations_active_target
    ON memory_relations (tenant_id, target_entity_id)
    WHERE is_active;

-- ═══════════════════════════════════════════════════════════════════════════
-- 4. memory_entities: CRM record, embedding, scope.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_entities
    ADD COLUMN IF NOT EXISTS crm_person_id  UUID,
    ADD COLUMN IF NOT EXISTS crm_company_id UUID,
    ADD COLUMN IF NOT EXISTS embedding      vector(1024),
    ADD COLUMN IF NOT EXISTS scope_kind     TEXT NOT NULL DEFAULT 'shared',
    ADD COLUMN IF NOT EXISTS scope_id       TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_entities'::regclass
                     AND conname = 'memory_entities_crm_person_id_fkey') THEN
        ALTER TABLE memory_entities ADD CONSTRAINT memory_entities_crm_person_id_fkey
            FOREIGN KEY (crm_person_id) REFERENCES crm_people(id) ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_entities'::regclass
                     AND conname = 'memory_entities_crm_company_id_fkey') THEN
        ALTER TABLE memory_entities ADD CONSTRAINT memory_entities_crm_company_id_fkey
            FOREIGN KEY (crm_company_id) REFERENCES crm_companies(id) ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_entities'::regclass
                     AND conname = 'memory_entities_scope_kind_check') THEN
        ALTER TABLE memory_entities ADD CONSTRAINT memory_entities_scope_kind_check
            CHECK (scope_kind IN ('shared', 'user', 'agent')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_entities'::regclass
                     AND conname = 'memory_entities_scope_id_check') THEN
        ALTER TABLE memory_entities ADD CONSTRAINT memory_entities_scope_id_check
            CHECK ((scope_kind = 'shared') = (scope_id IS NULL)) NOT VALID;
    END IF;
END $$;

-- The new columns are NULL (or the default) on every existing row, so
-- validating is one cheap pass over memory_entities and leaves no constraint
-- that a dump or the doctor would report as unvalidated.
ALTER TABLE memory_entities VALIDATE CONSTRAINT memory_entities_crm_person_id_fkey;
ALTER TABLE memory_entities VALIDATE CONSTRAINT memory_entities_crm_company_id_fkey;
ALTER TABLE memory_entities VALIDATE CONSTRAINT memory_entities_scope_kind_check;
ALTER TABLE memory_entities VALIDATE CONSTRAINT memory_entities_scope_id_check;
ALTER TABLE memory_relations VALIDATE CONSTRAINT memory_relations_retired_reason_check;
ALTER TABLE memory_relations VALIDATE CONSTRAINT memory_relations_origin_check;

-- Case-insensitive lookup by name (get_entity already filters on lower(name)).
CREATE INDEX IF NOT EXISTS idx_memory_entities_tenant_lower_name
    ON memory_entities (tenant_id, lower(name));
-- ON DELETE SET NULL from the CRM, and lookup of the entity for a CRM record.
CREATE INDEX IF NOT EXISTS idx_memory_entities_crm_person
    ON memory_entities (crm_person_id)
    WHERE crm_person_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_entities_crm_company
    ON memory_entities (crm_company_id)
    WHERE crm_company_id IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════════════════
-- 5. memory_facts: origin, event time, scope. Last, so its ACCESS EXCLUSIVE
--    lock is held only to the commit. Every statement here is metadata-only:
--    the columns are NULL or a constant default, and the CHECKs are NOT VALID
--    (they bind every new and updated row; existing rows already satisfy
--    them). No index, no scan, no rewrite.
--
--    event_time is when the thing happened. It is not valid_from (migration
--    095), which is when the fact became true as recorded.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS origin     TEXT,
    ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS scope_kind TEXT NOT NULL DEFAULT 'shared',
    ADD COLUMN IF NOT EXISTS scope_id   TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_facts'::regclass
                     AND conname = 'memory_facts_origin_check') THEN
        ALTER TABLE memory_facts ADD CONSTRAINT memory_facts_origin_check
            CHECK (origin IN ('operator_confirmed', 'user_stated', 'crm', 'tool',
                              'agent_generated', 'external')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_facts'::regclass
                     AND conname = 'memory_facts_scope_kind_check') THEN
        ALTER TABLE memory_facts ADD CONSTRAINT memory_facts_scope_kind_check
            CHECK (scope_kind IN ('shared', 'user', 'agent')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'memory_facts'::regclass
                     AND conname = 'memory_facts_scope_id_check') THEN
        ALTER TABLE memory_facts ADD CONSTRAINT memory_facts_scope_id_check
            CHECK ((scope_kind = 'shared') = (scope_id IS NULL)) NOT VALID;
    END IF;
END $$;
