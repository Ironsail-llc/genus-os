"""Remember a server-selected existing task before delivering a deduplication result."""

from robothor.engine.runtime import effects


def remember_existing(tenant_id, task_id, *, cursor=None):
    record = effects.active_effect.get()
    if record is None or record["tool_name"] != "create_task":
        return
    if record["tenant_id"] != tenant_id:
        raise ValueError("Task receipt tenant does not match effect admission")
    if cursor is not None:
        _bind(cursor, record, task_id)
        return
    with effects.get_connection() as conn, conn.cursor() as cur:
        _bind(cur, record, task_id)


def _bind(cur, record, task_id):
    cur.execute(
        """UPDATE agent_runtime_effects e
           SET resolution=jsonb_build_object('lookup',jsonb_build_object('task_id',t.id::text)),
               version=e.version+1,updated_at=now()
           FROM crm_tasks t WHERE e.id=%s AND e.tenant_id=%s AND e.run_id=%s
             AND e.principal_id=%s AND e.state='dispatching' AND e.tool_name='create_task'
             AND t.id=%s AND t.tenant_id=e.tenant_id AND t.deleted_at IS NULL
             AND (e.resolution IS NULL OR e.resolution->'lookup'->>'task_id'=t.id::text)""",
        (record["id"], record["tenant_id"], record["run_id"], record["principal_id"], task_id),
    )
    if cur.rowcount != 1:
        raise ValueError("Existing task receipt could not be bound to the active action")
