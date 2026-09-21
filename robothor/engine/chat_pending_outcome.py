"""Read durable request decisions while execution evidence is not yet available."""


def pending_outcome(cur, auth, session_key, identifier):
    cur.execute(
        """SELECT 1 FROM agent_runtime_request_stops
           WHERE tenant_id=%s AND request_id=%s""",
        (auth.tenant_id, identifier),
    )
    if cur.fetchone():
        # Absence of a run in the preceding read is not proof of no effects:
        # admission and an external dispatch may race with the stop commit.
        return {
            "state": "stopping",
            "terminal": False,
            "verified": False,
            "stop_requested": True,
            "source": "request_stop_record",
            "waiting_reason": "execution_evidence",
            "text": "Stop is recorded. Checking the original request for any actions already dispatched…",
        }
    cur.execute(
        """SELECT 1 FROM chat_sessions WHERE tenant_id=%s AND session_key=%s
           AND plan_state->>'status'='approved'
           AND plan_state->>'approval_request_id'=%s
           UNION ALL SELECT 1 FROM chat_approval_receipts
           WHERE tenant_id=%s AND session_key=%s AND request_id=%s LIMIT 1""",
        (auth.tenant_id, session_key, identifier, auth.tenant_id, session_key, identifier),
    )
    if cur.fetchone():
        return {
            "state": "accepted",
            "terminal": False,
            "verified": False,
            "source": "approval_record",
            "waiting_reason": "execution_admission",
            "text": "Your approval is recorded. Checking whether execution has started…",
        }
    return {"state": "not_found", "terminal": False}
