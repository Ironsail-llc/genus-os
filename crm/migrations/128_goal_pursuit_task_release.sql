-- A finished goal owns nothing, and a switched-off feature owns nothing.
--
-- A separate migration rather than an edit to 126/127: the canonical runner
-- keys an applied migration by SHA-256 and treats an edit as drift. This file
-- replaces one function and is re-runnable.
--
-- 126 wrote the predicate as:
--
--   AND (g.status IN ('paused','blocked','review','complete','canceled')
--        OR NOT COALESCE(s.enabled,false))
--
-- Two of those five states, and the enabled clause, take a task OUT of every
-- agent's inbox and never give it back:
--
--   complete / canceled  A goal reaching its natural end is the happy path.
--       Nothing in code, CLI, API or UI ever deletes a pursuit_goal_tasks row
--       (`grep 'DELETE FROM pursuit_goal_tasks'` found nothing), so a
--       long-term goal that linked twenty CRM tasks and then finished removed
--       all twenty from list_agent_tasks and thread_pool.list_threads
--       permanently -- while the tasks were still TODO and still assigned,
--       invisible to every agent, and recoverable only by a manual DELETE
--       against production. Nothing notified anybody.
--
--   NOT enabled  docs/features/goal-pursuit.md calls disabling the tenant
--       switch "the runtime rollback". A rollback that hides ordinary CRM
--       tasks from the agents they are assigned to is not a rollback. With
--       pursuit off no goal can be dispatched anyway (store.claim refuses),
--       so there is no coordinator whose work these tasks could collide with.
--
-- paused / blocked / review stay: those are live goals whose work is
-- deliberately held, the operator can see them in the Goals view, and
-- `resume` puts the tasks back. That is the case the predicate was written
-- for, and it still works.
CREATE OR REPLACE FUNCTION pursuit_task_runnable(task UUID, tenant TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM pursuit_goal_tasks l
        JOIN pursuit_goals g ON g.tenant_id=l.tenant_id AND g.id=l.goal_id
        WHERE l.task_id=task AND l.tenant_id=tenant
          AND g.status IN ('paused','blocked','review')
    );
$$;
