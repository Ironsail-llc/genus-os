-- Migration 181: warm-up's entity recall reads shared facts only.
--
-- Migration 176 gave memory_facts an owner (scope_kind / scope_id) and the
-- write path can store a fact private to one person, member or agent
-- (robothor/memory/visibility.py has the read-side rule). Warm-up's
-- name-matched entity context runs through entity_facts_for_candidates
-- (migration 166), which has no reader: it would inject one person's private
-- fact into anyone's turn that names the entity.
--
-- The function is replaced with the same signature and the same body plus
-- `m.scope_kind = 'shared'`, so a release that calls it keeps working and can
-- never see a private fact. Shared rows are unchanged: every row before
-- migration 176 is shared, and nothing writes private facts by default.
--
-- Idempotent: CREATE OR REPLACE of one function, grants re-applied.

CREATE OR REPLACE FUNCTION entity_facts_for_candidates(
    p_tenant_id text,
    p_candidates text[],
    p_restricted boolean,
    p_person_id uuid DEFAULT NULL
)
RETURNS TABLE (
    candidate_ordinal bigint,
    fact_text text,
    category varchar,
    importance_score double precision,
    person_id uuid,
    created_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT c.ord, f.fact_text, f.category, f.importance_score, f.person_id, f.created_at
    FROM unnest(p_candidates) WITH ORDINALITY AS c(name, ord)
    CROSS JOIN LATERAL (
        SELECT m.fact_text, m.category, m.importance_score, m.person_id, m.created_at,
               0.55 * COALESCE(m.importance_score, 0.5)
               + 0.45 * POWER(
                   0.5, EXTRACT(EPOCH FROM (now() - m.created_at)) / 3600.0 / 168.0
               ) AS rank_score
        FROM public.memory_facts m
        WHERE m.is_active = TRUE
          AND m.tenant_id = p_tenant_id
          AND m.scope_kind = 'shared'
          AND public.lower_entities(m.entities) && ARRAY[lower(c.name)]
          AND (NOT p_restricted OR m.person_id IS NULL OR m.person_id = p_person_id)
        ORDER BY rank_score DESC, m.created_at DESC
        LIMIT 3
    ) f
    WHERE COALESCE(current_setting('app.tenant_id', true), '') IN ('', p_tenant_id)
    ORDER BY c.ord, f.rank_score DESC, f.created_at DESC
$$;

REVOKE ALL ON FUNCTION entity_facts_for_candidates(text, text[], boolean, uuid) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'robothor_app') THEN
        GRANT EXECUTE ON FUNCTION entity_facts_for_candidates(text, text[], boolean, uuid)
            TO robothor_app;
    END IF;
END $$;
