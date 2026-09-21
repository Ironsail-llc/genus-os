-- Durable operator goals. Existing session goals are deliberately not enrolled.
CREATE TABLE IF NOT EXISTS goal_pursuit_settings (
    tenant_id TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT false,
    cursors JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pursuit_goals (
    tenant_id TEXT NOT NULL,
    id UUID NOT NULL,
    data JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('queued','running','waiting','blocked','paused','review','complete','canceled')),
    ready_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    priority INTEGER NOT NULL DEFAULT 0,
    lease_id UUID,
    lease_until TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS pursuit_goals_request_key
    ON pursuit_goals(tenant_id,(data->>'request_key')) WHERE data->>'request_key' IS NOT NULL;
CREATE INDEX IF NOT EXISTS pursuit_goals_ready ON pursuit_goals(tenant_id, status, ready_at);
CREATE UNIQUE INDEX IF NOT EXISTS pursuit_goals_one_coordinator
    ON pursuit_goals(tenant_id) WHERE lease_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS pursuit_goal_history (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    goal_id UUID NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, goal_id) REFERENCES pursuit_goals(tenant_id, id)
);
CREATE INDEX IF NOT EXISTS pursuit_goal_history_goal ON pursuit_goal_history(tenant_id,goal_id,id);
CREATE TABLE IF NOT EXISTS pursuit_goal_tasks (
    tenant_id TEXT NOT NULL,
    goal_id UUID NOT NULL,
    task_id UUID NOT NULL REFERENCES crm_tasks(id),
    PRIMARY KEY (tenant_id,goal_id,task_id),
    FOREIGN KEY (tenant_id,goal_id) REFERENCES pursuit_goals(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS pursuit_goal_attempts (
    tenant_id TEXT NOT NULL,
    id UUID NOT NULL,
    goal_id UUID NOT NULL,
    run_id UUID,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,goal_id) REFERENCES pursuit_goals(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS pursuit_goal_events (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id,id)
);
CREATE INDEX IF NOT EXISTS pursuit_goal_events_pending ON pursuit_goal_events(tenant_id,created_at)
    WHERE processed_at IS NULL;

-- Task changes and wake obligations commit together, including changes outside Python.
CREATE OR REPLACE FUNCTION pursuit_task_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pursuit_goal_tasks WHERE tenant_id=NEW.tenant_id AND task_id=NEW.id)
       OR EXISTS (SELECT 1 FROM pursuit_goals WHERE tenant_id=NEW.tenant_id
                  AND data->'wait'->>'task_id'=NEW.id::text) THEN
        INSERT INTO pursuit_goal_events(tenant_id,id,event_type,payload)
        VALUES (NEW.tenant_id,gen_random_uuid()::text,'task.changed',
                jsonb_build_object('task_id',NEW.id::text,'status',NEW.status));
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS pursuit_task_changed_trigger ON crm_tasks;
CREATE TRIGGER pursuit_task_changed_trigger AFTER UPDATE ON crm_tasks
FOR EACH ROW WHEN (OLD IS DISTINCT FROM NEW) EXECUTE FUNCTION pursuit_task_changed();

ALTER TABLE goal_pursuit_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_pursuit_settings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON goal_pursuit_settings;
CREATE POLICY tenant_isolation ON goal_pursuit_settings USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
ALTER TABLE pursuit_goals ENABLE ROW LEVEL SECURITY;
ALTER TABLE pursuit_goals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pursuit_goals;
CREATE POLICY tenant_isolation ON pursuit_goals USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
ALTER TABLE pursuit_goal_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE pursuit_goal_history FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pursuit_goal_history;
CREATE POLICY tenant_isolation ON pursuit_goal_history USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
ALTER TABLE pursuit_goal_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE pursuit_goal_tasks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pursuit_goal_tasks;
CREATE POLICY tenant_isolation ON pursuit_goal_tasks USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
ALTER TABLE pursuit_goal_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE pursuit_goal_attempts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pursuit_goal_attempts;
CREATE POLICY tenant_isolation ON pursuit_goal_attempts USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
ALTER TABLE pursuit_goal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE pursuit_goal_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pursuit_goal_events;
CREATE POLICY tenant_isolation ON pursuit_goal_events USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);

-- The ordinary task inbox must not dispatch work belonging to an inactive goal.
CREATE OR REPLACE FUNCTION pursuit_task_runnable(task UUID, tenant TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM pursuit_goal_tasks l
        JOIN pursuit_goals g ON g.tenant_id=l.tenant_id AND g.id=l.goal_id
        LEFT JOIN goal_pursuit_settings s ON s.tenant_id=g.tenant_id
        WHERE l.task_id=task AND l.tenant_id=tenant
          AND (g.status IN ('paused','blocked','review','complete','canceled') OR NOT COALESCE(s.enabled,false))
    );
$$;
