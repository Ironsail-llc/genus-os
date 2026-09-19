"""Exact, bounded revision manifests for complete business-history observations."""

import hashlib
import json
from datetime import datetime


def fingerprint(members):
    return hashlib.sha256(json.dumps(sorted(members), separators=(",", ":")).encode()).hexdigest()


def finish_scan(cur, tenant, scan, page, current):
    """Verify every committed page in this finite chain, including the leased last page."""
    if page.kind != "order" or page.next_cursor is not None:
        return None
    cur.execute(
        "SELECT payload,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.business' "
        "AND status='completed' AND payload->>'scan_id'=%s AND payload->>'source'=%s "
        "AND payload->>'account_id'=%s AND payload->>'practice_id'=%s LIMIT 1001",
        (tenant, scan.scan_id, scan.source, scan.account_id, scan.practice_id),
    )
    pages = [(r["payload"].get("after"), r["result"]) for r in cur.fetchall()]
    pages.append((scan.after, current))
    if len(pages) > 1000 or len({after for after, _ in pages}) != len(pages):
        return None
    by_cursor = dict(pages)
    cursor, members, proofs, visited = None, [], [], set()
    while cursor in by_cursor and cursor not in visited:
        visited.add(cursor)
        result = by_cursor[cursor]
        proof = result.get("coverage")
        if not proof:
            return None
        proofs.append(proof)
        members.extend(result.get("members", []))
        if len(members) > 10000:
            return None
        cursor = result.get("next_cursor")
        if cursor is None:
            break
    if cursor is not None or len(visited) != len(pages):
        return None
    proof = proofs[0]
    if any(
        p["fingerprint"] != proof["fingerprint"] or p["total"] != proof["total"] for p in proofs
    ):
        return None
    if len(members) != proof["total"] or len({m[0] for m in members}) != len(members):
        return None
    if fingerprint(members) != proof["fingerprint"]:
        return None
    return {
        "members": sorted(members),
        "through": min(datetime.fromisoformat(p["through"]) for p in proofs).isoformat(),
    }


def current_proof(cur, tenant, binding, orders):
    cur.execute(
        "SELECT result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.business' "
        "AND status='completed' AND payload->>'kind'='order' AND payload->>'source'=%s "
        "AND payload->>'account_id'=%s AND payload->>'practice_id'=%s "
        "AND result->>'final'='true' ORDER BY updated_at DESC,id DESC LIMIT 1",
        (tenant, binding["source"], binding["account_id"], binding["external_id"]),
    )
    row = cur.fetchone()
    proof = row["result"].get("complete_history") if row else None
    if not proof:
        return None
    members = dict(proof["members"])
    matching = [o for o in orders if o["external_id"] in members]
    if len(matching) != len(members) or any(
        o["revision"] != members[o["external_id"]] or o["data"]["fulfillment"] == "unknown"
        for o in matching
    ):
        return None
    # Later observations outside the snapshot invalidate it; old orphaned rows
    # remain evidence but cannot silently inflate the complete snapshot cohort.
    through = datetime.fromisoformat(proof["through"])
    if any(o["external_id"] not in members and o["observed_at"] > through for o in orders):
        return None
    return {"orders": matching, "through": through}
