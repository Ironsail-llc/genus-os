-- Goal pursuit: idle cost, referential integrity and event retention.
--
-- A separate migration rather than an edit to 126: the canonical runner keys
-- an applied migration by SHA-256 and treats an edited one as drift, and 126
-- is already applied where this feature runs. Everything here is additive and
-- the whole file is re-runnable.

-- 1. The task-change trigger fired for EVERY crm_tasks UPDATE on every
--    instance, and did two unindexed lookups before it could decide it had
--    nothing to record. Instances that never enable goal pursuit paid that on
--    every task write.
--
--    The settings row is a primary-key probe and answers "is this feature on
--    for this tenant at all" before anything else, and the two lookups behind
--    it now have indexes. Behaviour is unchanged while the feature is enabled.
--    While it is OFF, a task change no longer leaves a wake record behind: a
--    goal that was waiting on that task will therefore wake on its timed
--    fallback review after the tenant is switched back on, rather than
--    immediately. Every wait has such a fallback by construction, and no goal
--    can be dispatched while the switch is off in any case.
CREATE INDEX IF NOT EXISTS pursuit_goal_tasks_by_task
    ON pursuit_goal_tasks(tenant_id, task_id);
CREATE INDEX IF NOT EXISTS pursuit_goals_wait_task
    ON pursuit_goals(tenant_id, (data->'wait'->>'task_id'))
    WHERE data->'wait'->>'task_id' IS NOT NULL;

CREATE OR REPLACE FUNCTION pursuit_task_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM goal_pursuit_settings
                   WHERE tenant_id=NEW.tenant_id AND enabled) THEN
        RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM pursuit_goal_tasks WHERE tenant_id=NEW.tenant_id AND task_id=NEW.id)
       OR EXISTS (SELECT 1 FROM pursuit_goals WHERE tenant_id=NEW.tenant_id
                  AND data->'wait'->>'task_id'=NEW.id::text) THEN
        INSERT INTO pursuit_goal_events(tenant_id,id,event_type,payload)
        VALUES (NEW.tenant_id,gen_random_uuid()::text,'task.changed',
                jsonb_build_object('task_id',NEW.id::text,'status',NEW.status));
    END IF;
    RETURN NEW;
END $$;

-- 2. pursuit_goals.tenant_id had no foreign key while notify() writes to
--    crm_agent_notifications, which does. A typo'd tenant therefore produced
--    a goal whose every finish() rolled back on the notification insert and
--    held its lease forever. notify() is now non-fatal in its own savepoint
--    (see store.py); this closes the other end, so the goal cannot be created
--    in the first place.
--
--    Skipped, with a notice rather than a failure, if rows already reference a
--    tenant that is not in crm_tenants: an instance that has already created
--    such a goal should not be unable to migrate. Clean those rows up and
--    re-run this file.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pursuit_goals_tenant_fk') THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pursuit_goals g
               WHERE NOT EXISTS (SELECT 1 FROM crm_tenants t WHERE t.id=g.tenant_id)) THEN
        RAISE NOTICE 'pursuit_goals rows reference unknown tenants; tenant foreign key not added';
        RETURN;
    END IF;
    ALTER TABLE pursuit_goals
        ADD CONSTRAINT pursuit_goals_tenant_fk FOREIGN KEY (tenant_id) REFERENCES crm_tenants(id);
END $$;

-- 3. Event capture copied all eight bus streams into PostgreSQL every 60
--    seconds with no retention at all, and reconcile re-read up to 500
--    unprocessed rows on every claim. Capture is now filtered to the event
--    types some goal is actually waiting on (events.py), and these two tables
--    are trimmed on the same timer (store.prune). The index is what makes the
--    trim a range scan rather than a sequential one.
CREATE INDEX IF NOT EXISTS pursuit_goal_events_processed
    ON pursuit_goal_events(tenant_id, processed_at) WHERE processed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS pursuit_goal_history_age
    ON pursuit_goal_history(tenant_id, created_at);
