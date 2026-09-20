"""Durable per-request accounting for alternative runtime goal attempts.

Reservations and attempt totals commit before dispatch. Separate processes share
one ledger; constructing a new object never grants a fresh spending allowance.
"""

from robothor.goals import store
from robothor.goals.model import INACTIVE


class DurableAttemptBudget:
    def __init__(self, tenant, goal_id, attempt):
        self.tenant, self.goal_id, self.attempt = tenant, goal_id, attempt

    def _attempt(self, cur):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + self.tenant,))
        cur.execute(
            """SELECT a.tokens,a.status,g.data,g.lease_id,g.lease_until>now() AS live,s.enabled
               FROM pursuit_goal_attempts a JOIN pursuit_goals g
                 ON g.tenant_id=a.tenant_id AND g.id=a.goal_id
               JOIN goal_pursuit_settings s ON s.tenant_id=g.tenant_id
               WHERE a.tenant_id=%s AND a.id=%s AND a.goal_id=%s FOR UPDATE OF a,g""",
            (self.tenant, self.attempt, self.goal_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("goal attempt not found")
        return row

    @property
    def charged(self):
        with store.transaction() as cur:
            return self._attempt(cur)["tokens"]

    def _authorized_attempt(self, cur):
        row = self._attempt(cur)
        goal = row["data"]
        if (
            row["status"] != "running"
            or str(row["lease_id"]) != self.attempt
            or not row["live"]
            or not row["enabled"]
            or goal["status"] not in {"running", "queued"}
        ):
            raise ValueError("goal lease no longer authorizes provider spending")
        if goal["parent_goal_id"]:
            parent = store.locked(cur, self.tenant, goal["parent_goal_id"])
            if parent["status"] in INACTIVE:
                raise ValueError("goal family authority exhausted")
        return row

    def assert_tool_authorized(self):
        """Check durable authority again after provider work, before host dispatch."""
        with store.transaction() as cur:
            row = self._authorized_attempt(cur)
            if row["data"].get("recovery_required"):
                raise ValueError("goal requires reconciliation before business actions")

    def reserve(self, call_id, maximum):
        if not call_id or type(maximum) is not int or maximum <= 0:
            raise ValueError("call identity and positive bound required")
        with store.transaction() as cur:
            row = self._authorized_attempt(cur)
            goal = row["data"]
            cur.execute(
                """SELECT call_id,actual,reserved FROM pursuit_goal_provider_reservations
                   WHERE tenant_id=%s AND attempt_id=%s""",
                (self.tenant, self.attempt),
            )
            reservations = cur.fetchall()
            if any(r["call_id"] == call_id for r in reservations):
                raise ValueError("provider call already reserved; do not redispatch")
            if any(r["actual"] is not None and r["actual"] > r["reserved"] for r in reservations):
                raise ValueError("provider overrun closes further admission")
            budgets = [goal]
            if goal["parent_goal_id"]:
                budgets.append(store.locked(cur, self.tenant, goal["parent_goal_id"]))
            total = row["tokens"] + maximum
            if any(
                g["status"] in INACTIVE
                or (g["token_budget"] and g["tokens_used"] + total > g["token_budget"])
                for g in budgets
            ):
                raise ValueError("goal family budget or authority exhausted")
            cur.execute(
                """INSERT INTO pursuit_goal_provider_reservations
                   (tenant_id,attempt_id,call_id,reserved) VALUES (%s,%s,%s,%s)""",
                (self.tenant, self.attempt, call_id, maximum),
            )
            cur.execute(
                "UPDATE pursuit_goal_attempts SET tokens=%s WHERE tenant_id=%s AND id=%s",
                (total, self.tenant, self.attempt),
            )

    def settle(self, call_id, actual):
        if actual is not None and (type(actual) is not int or actual < 0):
            raise ValueError("invalid usage")
        overrun = False
        with store.transaction() as cur:
            attempt = self._attempt(cur)
            cur.execute(
                """SELECT reserved,actual FROM pursuit_goal_provider_reservations
                   WHERE tenant_id=%s AND attempt_id=%s AND call_id=%s FOR UPDATE""",
                (self.tenant, self.attempt, call_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("provider call was not reserved")
            if actual is None:
                return
            if row["actual"] is not None:
                if row["actual"] != actual:
                    raise ValueError("conflicting usage event")
                return
            if attempt["status"] != "running" or str(attempt["lease_id"]) != self.attempt:
                raise ValueError("closed attempt requires explicit usage reconciliation")
            cur.execute(
                """UPDATE pursuit_goal_provider_reservations SET actual=%s
                   WHERE tenant_id=%s AND attempt_id=%s AND call_id=%s""",
                (actual, self.tenant, self.attempt, call_id),
            )
            cur.execute(
                """UPDATE pursuit_goal_attempts SET tokens=tokens+%s
                   WHERE tenant_id=%s AND id=%s""",
                (actual - row["reserved"], self.tenant, self.attempt),
            )
            overrun = actual > row["reserved"]
        # Commit the actual charge before rejecting the response.
        if overrun:
            raise ValueError("provider exceeded reserved bound")
