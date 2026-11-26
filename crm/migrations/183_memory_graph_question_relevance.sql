-- Migration 183: the graph walk's facts, ranked by what the question means.
--
-- WHAT
--   memory_activate_for_question() runs the same bounded walk as
--   memory_activate (migration 178), then chooses each activated entity's
--   facts by how close they are in meaning to the question instead of by
--   importance:
--
--     * relevance is the cosine similarity of the fact's embedding to
--       p_query_embedding, the embedding of the question with the names it
--       links removed (robothor/memory/activation.py,
--       question_without_names). With the name left in, every fact naming
--       the entity looks most relevant and the reached entity's facts never
--       do. The reached set is bounded (200 entities), so the distance is
--       exact, computed over those facts only;
--     * only facts at or above p_min_similarity are returned: reached facts
--       unrelated to the question crowded the results;
--     * a question with nothing beyond its names (p_query has no word left
--       that full text would index, e.g. "Alice Smith" or "Who is Alice
--       Smith?") or without an embedding returns nothing: nothing is asked
--       that a neighbour could answer;
--     * an entity's facts are its p_facts_per_entity most similar ones; a
--       fact's score is (activation / strongest activation) ^ 0.25 *
--       similarity, so meaning decides and closeness in the graph weighs in;
--     * facts that mention an entity in p_named (the entities the question
--       itself names) are not returned: the dense and keyword legs find those
--       by name; the graph returns what they cannot;
--     * every row carries the columns of a fact leg's rows (confidence,
--       source_type, metadata, superseded_by, utility).
--
--   memory_activate itself is unchanged, so a release that calls it keeps
--   working (warm-up's automatic recall still does).
--
-- TENANCY, VISIBILITY, TRUST
--   SECURITY DEFINER with a fixed search_path, as memory_activate. The walk
--   is memory_activate's, which enforces tenant, reader visibility, person
--   scope, trust and operator exclusion itself; this function re-applies the
--   same fact rules to every fact it returns (tenant, active, shared or
--   user-scoped to p_reader_scope_id, the "own data + shared" person scope,
--   trusted origins when p_trusted_only), and itself refuses a session scoped
--   to another tenant.
--
-- Read-only. IDEMPOTENT: earlier drafts' signatures dropped, then CREATE OR
-- REPLACE, grants re-applied.
--
-- Rollback:
--   DROP FUNCTION IF EXISTS memory_activate_for_question(text, integer[],
--       double precision[], text, integer, integer, double precision, integer[], boolean,
--       integer, integer, boolean, uuid, uuid, text, vector, integer[], double precision);

-- Earlier drafts of this migration (unreleased) took a full-text query only;
-- a function's signature and return type cannot be replaced in place.
DROP FUNCTION IF EXISTS memory_activate_for_question(text, integer[], double precision[], text,
    integer, integer, double precision, integer[], boolean, integer, integer, boolean, uuid, uuid,
    text, integer[]);
DROP FUNCTION IF EXISTS memory_activate_for_question(text, integer[], double precision[], text,
    integer, integer, double precision, integer[], boolean, integer, integer, boolean, uuid, uuid,
    text, vector, integer[], double precision);

CREATE OR REPLACE FUNCTION memory_activate_for_question(
    p_tenant text,
    p_seeds integer[],
    p_seed_weights double precision[] DEFAULT NULL,
    p_reader_scope_id text DEFAULT NULL,
    p_max_hops integer DEFAULT 2,
    p_fanout integer DEFAULT 16,
    p_damping double precision DEFAULT 0.7,
    p_exclude integer[] DEFAULT '{}',
    p_trusted_only boolean DEFAULT true,
    p_limit integer DEFAULT 20,
    p_facts_per_entity integer DEFAULT 3,
    p_person_restricted boolean DEFAULT false,
    p_person_id uuid DEFAULT NULL,
    p_exclude_crm_person uuid DEFAULT NULL,
    p_query text DEFAULT NULL,
    p_query_embedding vector DEFAULT NULL,
    p_named integer[] DEFAULT '{}',
    p_min_similarity double precision DEFAULT 0.35
)
RETURNS TABLE (
    kind text,
    entity_id integer,
    entity_name text,
    score double precision,
    hops integer,
    via_edge_id integer,
    path_names text[],
    fact_id integer,
    fact_text text,
    category text,
    origin text,
    importance_score double precision,
    created_at timestamptz,
    person_id uuid,
    entities text[],
    access_count integer,
    relevance double precision,
    -- The remaining columns every fact leg returns, so a row the graph alone
    -- found has the same keys as any other fact row.
    confidence double precision,
    source_type text,
    metadata jsonb,
    superseded_by integer,
    utility double precision
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
DECLARE
    -- Activated entities whose facts are read, strongest first (as 178).
    c_entity_cap CONSTANT integer := 200;

    v_limit   integer := LEAST(GREATEST(COALESCE(p_limit, 20), 1), 500);
    v_per     integer := LEAST(GREATEST(COALESCE(p_facts_per_entity, 3), 1), 20);
    v_restr   boolean := COALESCE(p_person_restricted, false);
    v_trusted boolean := COALESCE(p_trusted_only, true);
    v_named   integer[] := COALESCE(p_named, '{}');
    v_floor   double precision := COALESCE(p_min_similarity, 0.35);
BEGIN
    IF p_tenant IS NULL OR p_tenant = ''
       OR COALESCE(current_setting('app.tenant_id', true), '') NOT IN ('', p_tenant) THEN
        RETURN;
    END IF;
    IF p_query_embedding IS NULL
       OR to_tsvector('english', COALESCE(p_query, '')) = ''::tsvector THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH walk AS MATERIALIZED (
        -- The walk: entity rows only, strongest first. One fact per entity is
        -- the least memory_activate computes; they are not used here.
        SELECT w.entity_id AS id, w.score AS s, w.hops AS hp, w.via_edge_id AS e
          FROM memory_activate(p_tenant, p_seeds, p_seed_weights, p_reader_scope_id,
                               p_max_hops, p_fanout, p_damping, p_exclude, v_trusted, 500, 1,
                               v_restr, p_person_id, p_exclude_crm_person) AS w
         WHERE w.kind = 'entity'
    ),
    -- The node each entity was first reached from: the far end of its edge.
    a AS MATERIALIZED (
        SELECT walk.id, walk.s, walk.hp, walk.e,
               CASE WHEN r.source_entity_id = walk.id THEN r.target_entity_id
                    ELSE r.source_entity_id END AS p
          FROM walk
          LEFT JOIN memory_relations r ON r.id = walk.e AND r.tenant_id = p_tenant
    ),
    ent AS MATERIALIZED (
        SELECT a.id, a.s, a.hp, a.e, a.p, me.name::text AS name
          FROM a
          JOIN memory_entities me ON me.id = a.id AND me.tenant_id = p_tenant
         ORDER BY a.s DESC, a.id
         LIMIT c_entity_cap
    ),
    top_s AS (
        SELECT GREATEST(max(ent.s), 1e-12) AS m FROM ent
    ),
    fx AS MATERIALIZED (
        SELECT ent.id AS ent_id, ent.s, ent.hp, ent.e, ent.name, bf.*
          FROM ent
          CROSS JOIN LATERAL (
              SELECT f.id AS fid, f.fact_text AS ftext, f.category::text AS fcat,
                     f.origin AS forigin, f.importance_score AS fimp,
                     f.created_at AS fcreated, f.person_id AS fperson,
                     f.entities AS fentities, f.access_count AS faccess,
                     f.confidence AS fconf, f.source_type::text AS fsrc,
                     f.metadata AS fmeta, f.superseded_by AS fsup, f.utility AS futil,
                     (1 - (f.embedding <=> p_query_embedding))::double precision AS frel
                FROM memory_mentions mm
                JOIN memory_facts f ON f.id = mm.fact_id
               WHERE mm.tenant_id = p_tenant
                 AND mm.entity_id = ent.id
                 AND f.tenant_id = p_tenant
                 AND f.is_active IS TRUE
                 AND (f.scope_kind = 'shared'
                      OR (f.scope_kind = 'user' AND f.scope_id = p_reader_scope_id))
                 AND (NOT v_restr OR f.person_id IS NULL OR f.person_id = p_person_id)
                 AND (NOT v_trusted
                      OR f.origin IN ('operator_confirmed', 'user_stated', 'crm'))
                 AND f.embedding IS NOT NULL
                 AND 1 - (f.embedding <=> p_query_embedding) >= v_floor
                 AND NOT EXISTS (
                      SELECT 1 FROM memory_mentions nm
                       WHERE nm.tenant_id = p_tenant
                         AND nm.fact_id = f.id
                         AND nm.entity_id = ANY (v_named))
               GROUP BY f.id
               ORDER BY frel DESC, f.importance_score DESC NULLS LAST,
                        f.created_at DESC NULLS LAST, f.id DESC
               LIMIT v_per
          ) AS bf
    ),
    per_fact AS (
        SELECT fx.fid,
               sum(fx.s) AS s,
               min(fx.hp) AS hp,
               max(fx.frel) AS rel,
               (array_agg(fx.ent_id ORDER BY fx.s DESC, fx.ent_id))[1] AS via
          FROM fx
         GROUP BY fx.fid
    ),
    -- Scored and cut before any path is built: the paths are read for the
    -- returned facts only.
    ranked AS MATERIALIZED (
        SELECT pf.*, power(LEAST(pf.s / top_s.m, 1.0), 0.25) * pf.rel AS sc
          FROM per_fact pf CROSS JOIN top_s
         ORDER BY sc DESC, pf.fid
         LIMIT v_limit
    )
    SELECT 'fact'::text, fx.ent_id, fx.name, rk.sc, rk.hp, fx.e,
           -- The path back to a seed, at most three steps, by the first-reach links.
           array_remove(ARRAY[n3.name::text, n2.name::text, n1.name::text, fx.name], NULL),
           fx.fid, fx.ftext, fx.fcat, fx.forigin, fx.fimp, fx.fcreated, fx.fperson,
           fx.fentities, fx.faccess, rk.rel, fx.fconf, fx.fsrc, fx.fmeta, fx.fsup, fx.futil
      FROM ranked rk
      JOIN fx ON fx.fid = rk.fid AND fx.ent_id = rk.via
      JOIN ent ON ent.id = fx.ent_id
      LEFT JOIN a a1 ON a1.id = ent.p
      LEFT JOIN memory_entities n1 ON n1.id = a1.id AND n1.tenant_id = p_tenant
      LEFT JOIN a a2 ON a2.id = a1.p
      LEFT JOIN memory_entities n2 ON n2.id = a2.id AND n2.tenant_id = p_tenant
      LEFT JOIN a a3 ON a3.id = a2.p
      LEFT JOIN memory_entities n3 ON n3.id = a3.id AND n3.tenant_id = p_tenant
     ORDER BY rk.sc DESC, fx.fimp DESC NULLS LAST, fx.fid;
END;
$$;

REVOKE ALL ON FUNCTION memory_activate_for_question(text, integer[], double precision[], text,
    integer, integer, double precision, integer[], boolean, integer, integer, boolean, uuid, uuid,
    text, vector, integer[], double precision) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'robothor_app') THEN
        GRANT EXECUTE ON FUNCTION memory_activate_for_question(text, integer[],
            double precision[], text, integer, integer, double precision, integer[], boolean,
            integer, integer, boolean, uuid, uuid, text, vector, integer[], double precision)
            TO robothor_app;
    END IF;
END $$;
