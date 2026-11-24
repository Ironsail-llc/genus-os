-- Migration 178: associative recall by bounded activation over the memory graph.
--
-- WHAT
--   memory_activate() spreads activation from a few seed entities across the
--   edges of memory_relations and returns the entities it reached and their
--   best facts, each with a graph score, its hop distance and the edge that
--   reached it. It is a forward-push approximation of Personalized PageRank:
--
--     * the seeds start with mass weight / sqrt(1 + visible degree), so a hub
--       seed starts weak (inverse-degree weighting);
--     * each hop pushes damping * mass from every frontier node to at most
--       p_fanout of its strongest edges (weight, then recency, then id), split
--       by edge weight over ALL its visible, eligible edges — a node with many
--       edges passes little to each;
--     * at most p_max_hops hops (hard cap 3); each hop is one set-based query
--       over the active-edge partial indexes of migration 176; the frontier
--       keeps its strongest FRONTIER_CAP nodes above EPSILON mass;
--     * edges are followed in both directions;
--     * an entity in p_exclude (the caller passes the operator's own node) is
--       never a seed and never receives or passes mass.
--
-- HUB CONTROL BY IDENTITY
--   p_exclude_crm_person excludes, as seed and transit, every entity of that
--   CRM person in the tenant: the caller passes the operator's person (from
--   get_owner_person), so the operator's node is resolved inside the walk.
--
-- VISIBILITY (the walk itself, not only its output)
--   An edge is walked only when at least one of its supporting facts is
--   visible to the reader, and only those edges count toward a node's degree
--   and weight total, so a path through evidence the reader cannot see
--   changes no score. A fact is visible when it is in p_tenant, active,
--   shared, or user-scoped (scope_kind 'user') to p_reader_scope_id — an
--   agent-scoped row is never a person reader's, whatever its id — and — when p_person_restricted —
--   about no one or about p_person_id (the existing "own data + shared" person
--   scope). An edge's evidence is the union of memory_relation_evidence and
--   the older memory_relations.fact_id column. Entities are visible when
--   shared or scoped to the reader. The facts returned for an activated
--   entity (through memory_mentions) obey the same rule.
--
-- TRUST (p_trusted_only, the automatic-recall mode)
--   An edge is eligible only when a visible supporting fact has origin
--   operator_confirmed, user_stated or crm, and the edge's own origin is not
--   inferred or external. Returned facts must themselves be of those origins.
--   NULL origin (every row written before migration 176) is agent-generated,
--   not trusted: over legacy data this mode returns nothing, by design.
--   With p_trusted_only false (a deliberate search) every origin is walked
--   and each fact comes back with its origin, so the caller can label it.
--   The origin lists are AUTO_RECALL_ORIGINS in robothor/memory/graph_schema.py.
--
-- TENANCY AND ROW-LEVEL SECURITY
--   As entity_facts_for_candidates (migration 166): SECURITY DEFINER with a
--   fixed search_path, because under the tenant_isolation policies the
--   planner may not push these predicates below the policy. Bypassing the
--   policy does not widen what a caller can read: every table access is
--   filtered by p_tenant inside the function — entities at both ends of an
--   edge, the edge, its evidence rows and facts, the mentions — and a
--   session scoped to another tenant (app.tenant_id set and not p_tenant)
--   gets nothing.
--
-- Read-only: no access counts, no writes.
--
-- IDEMPOTENT: CREATE OR REPLACE; safe to re-run and to renumber.
--
-- Rollback:
--   DROP FUNCTION IF EXISTS memory_activate(text, integer[], double precision[], text,
--       integer, integer, double precision, integer[], boolean, integer, integer, boolean, uuid,
--       uuid);
--   DROP FUNCTION IF EXISTS memory_activation_edges(text, integer[], text, boolean, uuid,
--       boolean, integer[]);

-- ═══════════════════════════════════════════════════════════════════════════
-- The visible, eligible edges of a set of nodes, one row per (node, edge),
-- with the neighbour at the far end. A plain SQL function so the planner
-- inlines it into each hop's query. Not SECURITY DEFINER itself: it is only
-- called from memory_activate, and runs as that function's owner. Every
-- relation is schema-qualified because it carries no search_path of its own.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION memory_activation_edges(
    p_tenant text,
    p_nodes integer[],
    p_reader_scope_id text,
    p_person_restricted boolean,
    p_person_id uuid,
    p_trusted_only boolean,
    p_exclude integer[]
)
RETURNS TABLE (node integer, edge_id integer, nbr integer, weight double precision,
               recorded timestamptz)
LANGUAGE sql
STABLE
AS $$
    WITH n AS (
        SELECT DISTINCT x AS node FROM unnest(p_nodes) AS x WHERE x IS NOT NULL
    ),
    e AS (
        SELECT n.node, r.id, r.target_entity_id AS nbr, r.weight,
               COALESCE(r.recorded_at, r.created_at) AS recorded, r.origin, r.fact_id
          FROM n
          JOIN public.memory_relations r
            ON r.tenant_id = p_tenant AND r.is_active AND r.source_entity_id = n.node
        UNION ALL
        SELECT n.node, r.id, r.source_entity_id, r.weight,
               COALESCE(r.recorded_at, r.created_at), r.origin, r.fact_id
          FROM n
          JOIN public.memory_relations r
            ON r.tenant_id = p_tenant AND r.is_active AND r.target_entity_id = n.node
    )
    SELECT e.node, e.id, e.nbr, e.weight, e.recorded
      FROM e
      JOIN public.memory_entities t
        ON t.id = e.nbr AND t.tenant_id = p_tenant
     WHERE e.nbr IS NOT NULL
       AND e.nbr <> e.node
       AND e.weight > 0
       AND NOT (e.nbr = ANY (p_exclude))
       AND (t.scope_kind = 'shared'
                    OR (t.scope_kind = 'user' AND t.scope_id = p_reader_scope_id))
       AND (NOT p_trusted_only OR e.origin IS NULL OR e.origin NOT IN ('inferred', 'external'))
       -- One correlated EXISTS over both evidence sources. Two EXISTS joined
       -- by OR let the planner hash the whole facts and evidence tables once
       -- the frontier grows (measured 15x slower at 256 frontier nodes); the
       -- correlation inside the UNION keeps it to index lookups per edge.
       AND EXISTS (
            SELECT 1
              FROM (
                  SELECT e.fact_id AS fid
                  UNION ALL
                  SELECT ev.fact_id
                    FROM public.memory_relation_evidence ev
                   WHERE ev.relation_id = e.id AND ev.tenant_id = p_tenant
              ) AS support
              JOIN public.memory_facts f ON f.id = support.fid
             WHERE f.tenant_id = p_tenant
               AND f.is_active IS TRUE
               AND (f.scope_kind = 'shared'
                    OR (f.scope_kind = 'user' AND f.scope_id = p_reader_scope_id))
               AND (NOT p_person_restricted OR f.person_id IS NULL OR f.person_id = p_person_id)
               AND (NOT p_trusted_only
                    OR f.origin IN ('operator_confirmed', 'user_stated', 'crm'))
       )
$$;

REVOKE ALL ON FUNCTION memory_activation_edges(text, integer[], text, boolean, uuid, boolean,
    integer[]) FROM PUBLIC;

-- ═══════════════════════════════════════════════════════════════════════════
-- The walk.
-- ═══════════════════════════════════════════════════════════════════════════

-- An earlier draft of this migration had no p_exclude_crm_person. Where it
-- ran, drop it so it cannot stay callable as an overload beside this one.
DROP FUNCTION IF EXISTS public.memory_activate(text, integer[], double precision[], text,
    integer, integer, double precision, integer[], boolean, integer, integer, boolean, uuid);

CREATE OR REPLACE FUNCTION memory_activate(
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
    p_exclude_crm_person uuid DEFAULT NULL
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
    access_count integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
-- Each query is planned for this call's frontier and flags: a cached generic
-- plan, chosen without knowing how many nodes or which mode, may hash whole
-- tables where a few index lookups do.
SET plan_cache_mode = force_custom_plan
AS $$
#variable_conflict use_column
DECLARE
    -- Frontier nodes below this mass stop spreading; at most this many spread
    -- per hop. Together with the fan-out cap they bound each hop's work to
    -- FRONTIER_CAP * fanout edge rows.
    c_epsilon      CONSTANT double precision := 1e-4;
    c_frontier_cap CONSTANT integer := 256;
    -- Activated entities whose facts are read, strongest first.
    c_entity_cap   CONSTANT integer := 200;

    v_hops    integer := LEAST(GREATEST(COALESCE(p_max_hops, 2), 0), 3);
    v_fanout  integer := LEAST(GREATEST(COALESCE(p_fanout, 16), 1), 256);
    v_damp    double precision := LEAST(GREATEST(COALESCE(p_damping, 0.7), 0), 1);
    v_limit   integer := LEAST(GREATEST(COALESCE(p_limit, 20), 1), 500);
    v_per     integer := LEAST(GREATEST(COALESCE(p_facts_per_entity, 3), 1), 20);
    v_excl    integer[] := COALESCE(p_exclude, '{}') || ARRAY(
                  SELECT me.id FROM memory_entities me
                   WHERE me.tenant_id = p_tenant
                     AND p_exclude_crm_person IS NOT NULL
                     AND me.crm_person_id = p_exclude_crm_person);
    v_restr   boolean := COALESCE(p_person_restricted, false);
    v_trusted boolean := COALESCE(p_trusted_only, true);

    -- Frontier: node and the mass it still has to spread.
    f_ids  integer[];
    f_mass double precision[];
    -- What this hop pushed: node, mass, the edge and node it came from.
    n_ids  integer[];
    n_mass double precision[];
    n_edge integer[];
    n_prev integer[];
    -- Accumulated activation: node, score, first hop reached, edge and node
    -- that first reached it.
    a_ids   integer[];
    a_score double precision[];
    a_hop   integer[];
    a_edge  integer[];
    a_prev  integer[];
    h integer;
BEGIN
    IF p_tenant IS NULL OR p_tenant = ''
       OR COALESCE(current_setting('app.tenant_id', true), '') NOT IN ('', p_tenant) THEN
        RETURN;
    END IF;

    -- Seeds: in the tenant, visible to the reader, not excluded; mass falls
    -- with the seed's visible, eligible degree.
    WITH s AS (
        SELECT u.id, max(COALESCE(u.w, 1.0)) AS w
          FROM unnest(p_seeds, p_seed_weights) AS u(id, w)
         WHERE u.id IS NOT NULL
         GROUP BY u.id
    ),
    vs AS (
        SELECT s.id, s.w
          FROM s
          JOIN memory_entities e ON e.id = s.id AND e.tenant_id = p_tenant
         WHERE s.w > 0
           AND NOT (s.id = ANY (v_excl))
           AND (e.scope_kind = 'shared'
                    OR (e.scope_kind = 'user' AND e.scope_id = p_reader_scope_id))
    ),
    deg AS (
        SELECT x.node, count(*) AS d
          FROM memory_activation_edges(p_tenant, ARRAY(SELECT id FROM vs), p_reader_scope_id,
                                       v_restr, p_person_id, v_trusted, v_excl) AS x
         GROUP BY x.node
    )
    SELECT array_agg(vs.id ORDER BY vs.id),
           array_agg(vs.w / sqrt(1.0 + COALESCE(deg.d, 0)) ORDER BY vs.id)
      INTO f_ids, f_mass
      FROM vs LEFT JOIN deg ON deg.node = vs.id;

    IF f_ids IS NULL THEN
        RETURN;
    END IF;

    a_ids := f_ids;
    a_score := f_mass;
    a_hop := array_fill(0, ARRAY[cardinality(f_ids)]);
    a_edge := array_fill(NULL::integer, ARRAY[cardinality(f_ids)]);
    a_prev := array_fill(NULL::integer, ARRAY[cardinality(f_ids)]);

    FOR h IN 1..v_hops LOOP
        EXIT WHEN f_ids IS NULL OR cardinality(f_ids) = 0;

        -- One hop: every frontier node pushes to its strongest edges, mass
        -- split over all its visible, eligible edges.
        WITH fr AS (
            SELECT u.node, u.mass FROM unnest(f_ids, f_mass) AS u(node, mass)
        ),
        ed AS (
            SELECT x.node, x.edge_id, x.nbr, x.weight, x.recorded, fr.mass
              FROM memory_activation_edges(p_tenant, f_ids, p_reader_scope_id, v_restr,
                                           p_person_id, v_trusted, v_excl) AS x
              JOIN fr ON fr.node = x.node
        ),
        rk AS (
            SELECT ed.*,
                   sum(ed.weight) OVER (PARTITION BY ed.node) AS wsum,
                   row_number() OVER (PARTITION BY ed.node
                                      ORDER BY ed.weight DESC, ed.recorded DESC NULLS LAST,
                                               ed.edge_id) AS rn
              FROM ed
        ),
        push AS (
            SELECT rk.nbr, v_damp * rk.mass * rk.weight / rk.wsum AS m, rk.edge_id, rk.node
              FROM rk
             WHERE rk.rn <= v_fanout
        ),
        agg AS (
            SELECT push.nbr,
                   sum(push.m) AS m,
                   (array_agg(push.edge_id ORDER BY push.m DESC, push.edge_id))[1] AS e,
                   (array_agg(push.node ORDER BY push.m DESC, push.edge_id))[1] AS p
              FROM push
             GROUP BY push.nbr
        )
        SELECT array_agg(agg.nbr ORDER BY agg.nbr), array_agg(agg.m ORDER BY agg.nbr),
               array_agg(agg.e ORDER BY agg.nbr), array_agg(agg.p ORDER BY agg.nbr)
          INTO n_ids, n_mass, n_edge, n_prev
          FROM agg;

        EXIT WHEN n_ids IS NULL;

        -- Fold the pushes into the activation; a node keeps the hop and edge
        -- that reached it first.
        WITH a AS (
            SELECT * FROM unnest(a_ids, a_score, a_hop, a_edge, a_prev) AS u(id, s, hp, e, p)
        ),
        n AS (
            SELECT * FROM unnest(n_ids, n_mass, n_edge, n_prev) AS u(id, m, e, p)
        ),
        j AS (
            SELECT COALESCE(a.id, n.id) AS id,
                   COALESCE(a.s, 0) + COALESCE(n.m, 0) AS s,
                   COALESCE(a.hp, h) AS hp,
                   CASE WHEN a.id IS NULL THEN n.e ELSE a.e END AS e,
                   CASE WHEN a.id IS NULL THEN n.p ELSE a.p END AS p
              FROM a FULL JOIN n ON n.id = a.id
        )
        SELECT array_agg(j.id ORDER BY j.id), array_agg(j.s ORDER BY j.id),
               array_agg(j.hp ORDER BY j.id), array_agg(j.e ORDER BY j.id),
               array_agg(j.p ORDER BY j.id)
          INTO a_ids, a_score, a_hop, a_edge, a_prev
          FROM j;

        -- The next frontier: what was just pushed, strongest first, bounded.
        SELECT array_agg(t.id ORDER BY t.id), array_agg(t.m ORDER BY t.id)
          INTO f_ids, f_mass
          FROM (
              SELECT u.id, u.m
                FROM unnest(n_ids, n_mass) AS u(id, m)
               WHERE u.m >= c_epsilon
               ORDER BY u.m DESC, u.id
               LIMIT c_frontier_cap
          ) AS t;
    END LOOP;

    RETURN QUERY
    WITH a AS (
        SELECT * FROM unnest(a_ids, a_score, a_hop, a_edge, a_prev) AS u(id, s, hp, e, p)
    ),
    ent AS (
        SELECT a.id, a.s, a.hp, a.e, a.p, me.name::text AS name
          FROM a
          JOIN memory_entities me ON me.id = a.id AND me.tenant_id = p_tenant
         ORDER BY a.s DESC, a.id
         LIMIT c_entity_cap
    ),
    -- The path back to a seed, at most three steps, by the first-reach links.
    paths AS (
        SELECT ent.id,
               array_remove(ARRAY[n3.name::text, n2.name::text, n1.name::text, ent.name],
                            NULL) AS names
          FROM ent
          LEFT JOIN a a1 ON a1.id = ent.p
          LEFT JOIN memory_entities n1 ON n1.id = a1.id AND n1.tenant_id = p_tenant
          LEFT JOIN a a2 ON a2.id = a1.p
          LEFT JOIN memory_entities n2 ON n2.id = a2.id AND n2.tenant_id = p_tenant
          LEFT JOIN a a3 ON a3.id = a2.p
          LEFT JOIN memory_entities n3 ON n3.id = a3.id AND n3.tenant_id = p_tenant
    ),
    fx AS (
        SELECT ent.id AS ent_id, ent.s, ent.hp, ent.e, ent.name, bf.*
          FROM ent
          CROSS JOIN LATERAL (
              SELECT f.id AS fid, f.fact_text AS ftext, f.category::text AS fcat,
                     f.origin AS forigin, f.importance_score AS fimp,
                     f.created_at AS fcreated, f.person_id AS fperson,
                     f.entities AS fentities, f.access_count AS faccess
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
               GROUP BY f.id
               ORDER BY f.importance_score DESC NULLS LAST, f.created_at DESC NULLS LAST,
                        f.id DESC
               LIMIT v_per
          ) AS bf
    ),
    -- A fact several activated entities mention sums their activation, and
    -- names the strongest of them as the way it was reached.
    per_fact AS (
        SELECT fx.fid,
               sum(fx.s) AS s,
               min(fx.hp) AS hp,
               (array_agg(fx.ent_id ORDER BY fx.s DESC, fx.ent_id))[1] AS via
          FROM fx
         GROUP BY fx.fid
    )
    SELECT 'fact'::text, fx.ent_id, fx.name, pf.s, pf.hp, fx.e, paths.names,
           fx.fid, fx.ftext, fx.fcat, fx.forigin, fx.fimp, fx.fcreated, fx.fperson,
           fx.fentities, fx.faccess
      FROM per_fact pf
      JOIN fx ON fx.fid = pf.fid AND fx.ent_id = pf.via
      JOIN paths ON paths.id = fx.ent_id
     ORDER BY pf.s DESC, fx.fimp DESC NULLS LAST, fx.fid
     LIMIT v_limit;

    -- The activated entities themselves, strongest first.
    RETURN QUERY
    SELECT 'entity'::text, a.id, me.name::text, a.s, a.hp, a.e, NULL::text[],
           NULL::integer, NULL::text, NULL::text, NULL::text, NULL::double precision,
           NULL::timestamptz, NULL::uuid, NULL::text[], NULL::integer
      FROM unnest(a_ids, a_score, a_hop, a_edge) AS a(id, s, hp, e)
      JOIN memory_entities me ON me.id = a.id AND me.tenant_id = p_tenant
     ORDER BY a.s DESC, a.id
     LIMIT v_limit;
END;
$$;

REVOKE ALL ON FUNCTION memory_activate(text, integer[], double precision[], text, integer,
    integer, double precision, integer[], boolean, integer, integer, boolean, uuid, uuid)
    FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'robothor_app') THEN
        GRANT EXECUTE ON FUNCTION memory_activate(text, integer[], double precision[], text,
            integer, integer, double precision, integer[], boolean, integer, integer, boolean,
            uuid, uuid) TO robothor_app;
    END IF;
END $$;
