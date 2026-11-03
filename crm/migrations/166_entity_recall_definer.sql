-- Migration 166: entity recall that can use its index under row-level security.
--
-- `_fetch_entity_facts_batched` (robothor/engine/warmup.py) looks up the
-- interactive warmup's entity candidates with
--     lower_entities(entities) && ARRAY[lower(name)]
-- which migration 109 indexed (idx_facts_entities_lower). Run as the table
-- owner it takes about 20 ms. Run as the engine's RLS-bound role it took
-- 0.8-1.3 s: under the tenant_isolation policy (migration 081) the planner
-- may only push a predicate below the policy when every function and
-- operator in it is LEAKPROOF. Neither lower_entities() nor the array
-- overlap operator `&&` is, so the match was applied as a filter after the
-- policy, over an index scan of the tenant's whole fact set, once per
-- candidate. Marking lower_entities LEAKPROOF alone does not change that.
--
-- entity_facts_for_candidates runs the same query as SECURITY DEFINER, so it
-- executes as its owner. The owner must bypass RLS (a superuser or a
-- BYPASSRLS role, which is what runs migrations); owned by a role that does
-- not, the function stays correct and is simply no faster.
--
-- Bypassing the policy must not widen what a caller can read, so the
-- function enforces the policy's own rule itself:
--   * it returns only rows of p_tenant_id, and
--   * when the session is scoped (app.tenant_id is set and non-empty), only
--     if p_tenant_id is that tenant; another tenant's id returns nothing.
-- That second check compares a parameter with a setting, not a row, so it
-- costs the planner nothing.
--
-- p_restricted keeps the restricted-caller filter the inline query had: the
-- facts of p_person_id and facts about no one. A restricted caller with no
-- linked person (p_person_id NULL) gets only facts about no one; restricted
-- is never inferred from a missing person.
--
-- Rows come back per candidate (WITH ORDINALITY, in the order given), at most
-- three each, ranked by the recency/importance blend
--     0.55 * importance + 0.45 * 0.5 ^ (age in hours / 168)
-- with newer facts first on a tie.

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
