BEGIN;

-- Task dispatch must honor the same inactive ancestors as goal admission.
-- UNION bounds recursion even if historical data contains a parent cycle.
CREATE OR REPLACE FUNCTION pursuit_task_runnable(task UUID, tenant TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    WITH RECURSIVE family AS (
        SELECT g.id, g.status, g.data->>'parent_goal_id' AS parent_id
        FROM pursuit_goal_tasks l
        JOIN pursuit_goals g ON g.tenant_id=l.tenant_id AND g.id=l.goal_id
        WHERE l.task_id=task AND l.tenant_id=tenant
        UNION
        SELECT p.id, p.status, p.data->>'parent_goal_id'
        FROM pursuit_goals p
        JOIN family f ON p.id::text=f.parent_id
        WHERE p.tenant_id=tenant
    )
    SELECT NOT EXISTS (
        SELECT 1 FROM family f
        LEFT JOIN goal_pursuit_settings s ON s.tenant_id=tenant
        WHERE f.status IN ('paused','blocked','review','complete','canceled')
           OR NOT COALESCE(s.enabled,false)
           OR (f.parent_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM pursuit_goals p
               WHERE p.tenant_id=tenant AND p.id::text=f.parent_id
           ))
    );
$$;

COMMIT;
