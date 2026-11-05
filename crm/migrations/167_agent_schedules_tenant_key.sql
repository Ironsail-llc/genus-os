-- Migration 167: agent_schedules is keyed per tenant.
--
-- The table is under the tenant_isolation policy (migration 081), yet its
-- primary key was agent_id alone, and tenants share job ids: every tenant
-- engine declares the same stock agents. The first tenant to write a job id
-- owned it. Every other tenant's upsert met that row through ON CONFLICT and
-- the policy refused the update, so the scheduler logged a warning on each
-- start and reload, kept the job in memory, and no row, last-run state or
-- error count ever existed for that job in that tenant.
--
-- The key becomes (tenant_id, agent_id). No row is deleted or re-tenanted. A
-- NULL tenant_id (the column was nullable, and a primary key cannot be) takes
-- the column default, which is the tenant the row was already read as.
--
-- LIVE TABLE. The swap takes ACCESS EXCLUSIVE, briefly: the table holds one
-- row per scheduled job. The lock is taken first, so the UPDATE below cannot
-- deadlock upgrading its own lock against a concurrent writer, and lock_timeout
-- bounds the wait, so a long transaction fails this migration cleanly (the
-- runner rolls it back and a retry starts over) instead of queueing every
-- schedule write behind it.
--
-- RE-RUN. The whole body is guarded on the key being the old one, so a renumber
-- or a re-application takes no lock and changes nothing, including once two
-- tenants hold the same job id, when the old key could not be rebuilt anyway.
--
-- ROLLBACK. The number sits above every migration an older release ships, so
-- that release reads it as AHEAD and starts; no ledger row needs deleting. There
-- is no down migration: restoring the old key would mean choosing which tenant's
-- row survives. Older code beside this key fails its ON CONFLICT (agent_id)
-- upserts with a logged warning and keeps its jobs in memory, the same degraded
-- state this migration fixes, and rows it already wrote stay readable. See
-- docs/deployment.md, "Rolling back to an older release".
--
-- No foreign key references this table, so nothing else moves with the key.

BEGIN;

SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_index x
        JOIN pg_attribute a
          ON a.attrelid = x.indrelid AND a.attnum = x.indkey[0]
        WHERE x.indrelid = 'public.agent_schedules'::regclass
          AND x.indisprimary
          AND x.indnkeyatts = 1
          AND a.attname = 'agent_id'
    ) THEN
        LOCK TABLE public.agent_schedules IN ACCESS EXCLUSIVE MODE;

        UPDATE public.agent_schedules SET tenant_id = DEFAULT WHERE tenant_id IS NULL;

        ALTER TABLE public.agent_schedules ALTER COLUMN tenant_id SET NOT NULL;

        ALTER TABLE public.agent_schedules
            DROP CONSTRAINT agent_schedules_pkey,
            ADD CONSTRAINT agent_schedules_pkey PRIMARY KEY (tenant_id, agent_id);
    END IF;
END $$;

COMMIT;
