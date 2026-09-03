#!/usr/bin/env python3
"""Two-instance federation soak — the ship gate.

Everything smaller than this has been passed by a system that carried zero
federation messages for five months. `test_nats_request.py` was green the whole
time because it mocks the manager and calls a function production never
reached; `federation status` printed `active` for a link whose transport did
not exist. So the gate is deliberately made of things that cannot be faked:

  - two SEPARATE databases. Two tenants in one database would let a whole
    class of bug through, because a missing tenant predicate would still
    return the right rows.
  - two SEPARATE processes, each running the real `_start_federation`. The
    daemon is what was broken, so the daemon has to be what attaches.
  - a real nats-server with the generated account config.
  - a RESTART of both sides, because attaching once from a warm object proves
    nothing about attaching from persisted state.
  - a negative half run three times, each with two of the three layers
    disabled, because "it refused" is not evidence of three layers when one
    layer plus two decorations refuses identically.

Every check prints PASS / FAIL / BLOCKED. BLOCKED means a precondition this
script cannot satisfy (no LLM credential, for instance) — it is never counted
as a pass, and the exit code distinguishes it from FAIL.

    python scripts/federation_soak.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEMPLATE_DB = os.environ.get("ROBOTHOR_SOAK_TEMPLATE", "robothor_test")
PARENT_DB = "soak_parent"
CHILD_DB = "soak_child"
ENGINE_PASSWORD = "soak-engine-pw"

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {PASS: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m", BLOCKED: "\033[33m—\033[0m"}[status]
    print(f"  {mark} {name}" + (f"\n      {detail}" if detail else ""), flush=True)


def check(name: str, condition: bool, detail: str = "") -> bool:
    record(PASS if condition else FAIL, name, "" if condition else detail)
    return condition


# ── Infrastructure ───────────────────────────────────────────────────


def psql(db: str, sql: str) -> str:
    out = subprocess.run(
        ["psql", "-d", db, "-tAc", sql], capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql on {db} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def recreate_databases() -> None:
    for db in (PARENT_DB, CHILD_DB):
        subprocess.run(
            ["psql", "-d", "postgres", "-qc", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'],
            capture_output=True,
            text=True,
        )
        # A connection can reappear between the terminate above and the
        # create below (a pool reconnecting, a parallel test run). Retry
        # rather than fail the whole gate on a race.
        made = None
        for attempt in range(5):
            made = subprocess.run(
                [
                    "psql",
                    "-d",
                    "postgres",
                    "-qc",
                    f'CREATE DATABASE "{db}" TEMPLATE "{TEMPLATE_DB}"',
                ],
                capture_output=True,
                text=True,
            )
            if made.returncode == 0:
                break
            if "being accessed" not in made.stderr:
                break
            subprocess.run(
                [
                    "psql",
                    "-d",
                    "postgres",
                    "-qc",
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{TEMPLATE_DB}' AND pid <> pg_backend_pid()",
                ],
                capture_output=True,
                text=True,
            )
            time.sleep(0.5 * (attempt + 1))
        if made is None or made.returncode != 0:
            raise RuntimeError(f"could not create {db}: {made.stderr.strip() if made else '?'}")
        # Two instances must not share history — and the soak must measure
        # only what it did. The template carries rows from the unit suite,
        # some with a NULL started_at, which made "the most recent federation
        # run" an arbitrary pick.
        psql(db, "TRUNCATE federation_connections CASCADE")
        psql(db, "TRUNCATE agent_runs CASCADE")
        psql(db, "DELETE FROM audit_log WHERE event_type = 'federation.op'")
        psql(db, "DELETE FROM user_permissions WHERE user_id LIKE 'federation:%'")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_broker(
    workdir: Path, connection_id: str, peer_password: str
) -> tuple[subprocess.Popen, int, Path]:
    from robothor.federation.nats_config import PeerAccount, render_config

    port = free_port()
    conf = workdir / "nats-server.conf"
    log = workdir / "nats-server.log"
    conf.write_text(
        render_config(
            listen=f"127.0.0.1:{port}",
            engine_password=ENGINE_PASSWORD,
            peers=[PeerAccount(connection_id, password=peer_password)],
        )
    )
    proc = subprocess.Popen(
        ["nats-server", "-c", str(conf), "-l", str(log), "-DV"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, port, log
        time.sleep(0.05)
    raise RuntimeError(f"nats-server did not start:\n{log.read_text()}")


def side(role: str, db: str, workdir: Path, **kwargs) -> subprocess.CompletedProcess:
    """Run one instance's step in its OWN process, with its OWN database."""
    env = {
        **os.environ,
        "ROBOTHOR_DB_NAME": db,
        "ROBOTHOR_SOAK_ARGS": json.dumps(kwargs),
        "PYTHONPATH": str(REPO),
    }
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--role", role, "--workdir", str(workdir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# ── The steps a side performs, each in its own process ───────────────


def _args() -> dict:
    return json.loads(os.environ.get("ROBOTHOR_SOAK_ARGS", "{}"))


def _config(workdir: Path, name: str):
    from robothor.federation.config import FederationConfig

    # Must match where FederationConfig.from_env() looks, because the
    # listener step builds its config exactly as the daemon does.
    d = workdir / name / ".robothor"
    d.mkdir(parents=True, exist_ok=True)
    return FederationConfig(
        instance_name=name,
        public_endpoint=f"nats://{name}.soak:4222",
        config_dir=d,
        identity_file=d / "identity.json",
    )


def role_init(workdir: Path) -> None:
    from robothor.federation.identity import init_identity

    a = _args()
    identity = init_identity(_config(workdir, a["name"]), display_name=a["name"])
    print(json.dumps({"instance_id": identity.id}))


def role_invite(workdir: Path) -> None:
    """The CHILD issues the invite: 'you are my parent'.

    That is the operator's organisational shape — the parent instance becomes
    a principal inside the subordinate one, rather than the subordinate
    reaching up.
    """
    from robothor.federation.identity import create_invite_token
    from robothor.federation.models import Relationship

    a = _args()
    invite = create_invite_token(
        _config(workdir, a["name"]), relationship=Relationship.CHILD, ttl_hours=1
    )
    print(
        json.dumps(
            {
                "token": invite.token,
                "connection_id": invite.connection_id,
                "secret": invite.connection_secret,
            }
        )
    )


def role_accept(workdir: Path) -> None:
    from robothor.federation.connections import save_connection
    from robothor.federation.identity import consume_invite_token

    a = _args()
    conn = consume_invite_token(_config(workdir, a["name"]), a["token"])
    conn.transport = {
        "kind": "nats",
        "url": a["nats_url"],
        "user": a["user"],
        "password": a["password"],
    }
    save_connection(conn)
    print(
        json.dumps(
            {
                "connection_id": conn.id,
                "relationship": conn.relationship.value,
                "role_granted_to_peer": conn.local_principal_role,
            }
        )
    )


def role_listen(workdir: Path) -> None:
    """The child's DAEMON attaches. This is the real `_start_federation`."""
    import asyncio

    from robothor.engine.config import EngineConfig
    from robothor.engine.daemon import _start_federation

    a = _args()
    os.environ["ROBOTHOR_NATS_URL"] = a["nats_url"]
    os.environ["ROBOTHOR_WORKSPACE"] = str(workdir / a["name"])
    os.environ["ROBOTHOR_NATS_USER"] = "engine"
    os.environ["ROBOTHOR_NATS_PASSWORD"] = ENGINE_PASSWORD

    os.environ["ROBOTHOR_INSTANCE_ID"] = a["instance_id"]

    async def main() -> None:
        # Built from the environment exactly as the daemon builds it — the
        # point of this step is that the real startup path attaches, so the
        # config must come from the same place it does in production.
        config = EngineConfig.from_env()
        runner = _SoakRunner()
        mgr = await _start_federation(config, runner=runner)
        from robothor.federation.transport import get_transport

        transport = get_transport()
        print(
            json.dumps(
                {
                    "manager": mgr is not None,
                    "transport": transport is not None,
                    "attached": transport.attached() if transport else [],
                }
            ),
            flush=True,
        )
        if mgr is None or transport is None:
            sys.exit(3)
        # Stay up so the peer has something to talk to.
        while True:
            await asyncio.sleep(3600)

    asyncio.run(main())


class _SoakRunner:
    """A runner that persists a real agent_runs row and does not call an LLM.

    The artifact under test is that the CHILD records WHICH principal triggered
    it. Executing a real agent would additionally need an LLM credential, which
    is a separate precondition — so this records the row the authorization
    model is supposed to produce and stops there. It is not a stub of the thing
    being proved: the principal it writes is the one the responder passed it.
    """

    async def execute(self, agent_id: str, message: str = "", **kwargs):
        import uuid

        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.tracking import create_run

        run = AgentRun(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            trigger_type=TriggerType.FEDERATION,
            trigger_detail=kwargs.get("trigger_detail", "")[:200],
            status=RunStatus.COMPLETED,
            user_id=kwargs.get("user_id", ""),
            user_role=kwargs.get("user_role", ""),
            tenant_id=kwargs.get("tenant_id", "default"),
        )
        create_run(run)
        return run


def role_pair(workdir: Path) -> None:
    """The parent dials in and completes the handshake."""
    import asyncio

    from robothor.federation.connections import load_connections, save_connection
    from robothor.federation.handshake import build_handshake, verify_ack
    from robothor.federation.transport import FederationTransport

    a = _args()
    config = _config(workdir, a["name"])

    async def main() -> None:
        conns = [c for c in load_connections() if c.id == a["connection_id"]]
        if not conns:
            print(json.dumps({"error": "no connection row on the dialer side"}))
            sys.exit(3)
        conn = conns[0]
        transport = FederationTransport(hub_url="")
        ok = await transport.attach(conn, pending_ok=True)
        if not ok:
            print(json.dumps({"error": "could not dial the peer"}))
            sys.exit(3)
        try:
            reply = await transport.request(
                conn.id, build_handshake(config, conn, a["secret"]), timeout=10.0
            )
            if reply is None:
                print(json.dumps({"error": "no reply"}))
                sys.exit(3)
            if b"error" in reply:
                print(json.dumps({"error": reply.decode()[:400]}))
                sys.exit(0 if a.get("expect_refusal") else 3)
            if not verify_ack(conn, reply):
                print(json.dumps({"error": "ack did not verify"}))
                sys.exit(3)
            save_connection(conn)
            print(json.dumps({"state": conn.state.value, "peer_id": conn.peer_id}))
        finally:
            await transport.close()

    asyncio.run(main())


def role_request(workdir: Path) -> None:
    """The parent makes a real op request against the child."""
    import asyncio

    from robothor.federation.connections import load_connections
    from robothor.federation.transport import FederationTransport

    a = _args()

    async def main() -> None:
        conn = next((c for c in load_connections() if c.id == a["connection_id"]), None)
        if conn is None:
            print(json.dumps({"error": "no connection"}))
            sys.exit(3)
        transport = FederationTransport(hub_url="")
        if not await transport.attach(conn):
            print(json.dumps({"error": "not attached"}))
            sys.exit(3)
        try:
            reply = await transport.request(
                conn.id, json.dumps(a["payload"]).encode(), timeout=10.0
            )
            print(json.dumps({"reply": reply.decode() if reply else None}))
        finally:
            await transport.close()

    asyncio.run(main())


def role_reverse(workdir: Path) -> None:
    """The CHILD tries to reach the PARENT. This must be refused by the broker.

    The child holds a valid credential for its own link. If account isolation
    and the per-user allow-list are real, it still cannot address any other
    subject — and nats-server logs the refusal.
    """
    import asyncio

    from robothor.federation.nats import NATSManager

    a = _args()

    async def main() -> None:
        client = NATSManager(
            a["nats_url"], user=a["user"], password=a["password"], allow_reconnect=False
        )
        if not await client.connect():
            print(json.dumps({"connected": False}))
            return
        try:
            import nats.errors

            try:
                await client._nc.request(a["subject"], b'{"op":"trigger"}', timeout=2.0)
                print(json.dumps({"refused": False, "reason": "the request was ANSWERED"}))
            except (TimeoutError, nats.errors.NoRespondersError, nats.errors.Error) as e:
                print(json.dumps({"refused": True, "error": type(e).__name__}))
            await asyncio.sleep(0.4)
        finally:
            await client.disconnect()

    asyncio.run(main())


ROLES = {
    "init": role_init,
    "invite": role_invite,
    "accept": role_accept,
    "listen": role_listen,
    "pair": role_pair,
    "request": role_request,
    "reverse": role_reverse,
}


# ── The soak ─────────────────────────────────────────────────────────


def out(proc: subprocess.CompletedProcess) -> dict:
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except Exception:
            continue
    return {"_stderr": proc.stderr[-1500:], "_stdout": proc.stdout[-500:]}


def soak() -> int:
    if shutil.which("nats-server") is None:
        record(BLOCKED, "nats-server available", "install nats-server to run the soak")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="fed-soak-"))
    broker = None
    listener = None
    print(f"\n\033[1mFederation soak\033[0m  workdir={workdir}\n")

    try:
        # ── 1. Two instances, two databases ──────────────────────────
        print("\033[1m1. Two instances, two databases\033[0m")
        recreate_databases()
        check("two separate databases exist", True, "")
        parent_id = out(side("init", PARENT_DB, workdir, name="parent")).get("instance_id")
        child_id = out(side("init", CHILD_DB, workdir, name="child")).get("instance_id")
        check(
            "each instance has its own identity",
            bool(parent_id and child_id and parent_id != child_id),
        )

        # ── 2. Pairing ───────────────────────────────────────────────
        print("\n\033[1m2. Pairing\033[0m")
        inv = out(side("invite", CHILD_DB, workdir, name="child"))
        connection_id, secret = inv.get("connection_id"), inv.get("secret")
        if not check(
            "the child issued an invite carrying a connection id", bool(connection_id), str(inv)
        ):
            return 1

        issuer_rows = int(
            psql(
                CHILD_DB, f"SELECT count(*) FROM federation_connections WHERE id='{connection_id}'"
            )
        )
        check(
            "issuing the invite persisted the issuer's side",
            issuer_rows == 1,
            "v1 wrote no row at all, so the invite could never be redeemed",
        )

        from robothor.federation.nats_config import PeerAccount, command_subject

        peer = PeerAccount(connection_id)
        broker, port, nats_log = start_broker(workdir, connection_id, peer.password)
        nats_url = f"nats://127.0.0.1:{port}"
        check("a real nats-server is running with the generated config", broker.poll() is None)

        acc = out(
            side(
                "accept",
                PARENT_DB,
                workdir,
                name="parent",
                token=inv["token"],
                nats_url=nats_url,
                user=peer.user,
                password=peer.password,
            )
        )
        check("the parent consumed the invite", acc.get("connection_id") == connection_id, str(acc))
        check(
            "both sides named the SAME connection",
            psql(
                PARENT_DB, f"SELECT count(*) FROM federation_connections WHERE id='{connection_id}'"
            )
            == "1",
            "v1 minted a fresh uuid on the consumer, so the subjects never lined up",
        )
        check(
            "the parent granted its child the deny-all role",
            acc.get("role_granted_to_peer") == "federation_child",
            f"got {acc.get('role_granted_to_peer')!r}",
        )
        check(
            "the child granted its parent the read-only role",
            psql(
                CHILD_DB,
                f"SELECT local_principal_role FROM federation_connections WHERE id='{connection_id}'",
            )
            == "federation_parent",
        )

        # ── 3. The daemon attaches, and the handshake activates ──────
        print("\n\033[1m3. Activation is the handshake, over the wire\033[0m")
        listener = start_listener(workdir, nats_url, child_id)
        first = read_json_line(listener)
        check(
            "the child's DAEMON attached the connection", bool(first.get("transport")), str(first)
        )
        check(
            "a PENDING connection is attached for pairing only",
            first.get("attached") == [connection_id],
            str(first),
        )

        # Before pairing succeeds: prove the deployment gate is armed. With RLS
        # inert the `tenant_scope` around every inbound op enforces nothing, so
        # admitting a remote principal would ship the third layer as a comment.
        # A control that has never refused anything is not a control.
        blocked = out(
            side(
                "pair",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                secret=secret,
                expect_refusal=True,
            )
        )
        check(
            "activation is REFUSED while row-level security is inert",
            "ROBOTHOR_RLS_ENABLED" in str(blocked.get("error", "")),
            "the RLS gate did not fire: " + str(blocked)[:300],
        )
        check(
            "the refusal left the connection PENDING",
            psql(CHILD_DB, f"SELECT state FROM federation_connections WHERE id='{connection_id}'")
            == "pending",
        )

        # Now the operator's deliberate override, and pairing proceeds.
        listener.terminate()
        listener.wait(timeout=10)
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        read_json_line(listener)
        paired = out(
            side(
                "pair",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                secret=secret,
            )
        )
        if not check(
            "the handshake completed over the real transport",
            paired.get("state") == "active",
            str(paired),
        ):
            return 1
        check(
            "the child activated too — both sides, not just the dialer",
            psql(CHILD_DB, f"SELECT state FROM federation_connections WHERE id='{connection_id}'")
            == "active",
        )
        for db, label in ((PARENT_DB, "parent"), (CHILD_DB, "child")):
            key = psql(
                db,
                f"SELECT length(peer_public_key) FROM federation_connections WHERE id='{connection_id}'",
            )
            check(f"the {label} learned its peer's public key", key not in ("", "0"))
        check(
            "activation was recorded with a timestamp",
            psql(
                CHILD_DB,
                f"SELECT activated_at IS NOT NULL FROM federation_connections WHERE id='{connection_id}'",
            )
            == "t",
        )

        # ── 4. Restart ───────────────────────────────────────────────
        print("\n\033[1m4. Restart — attaching from persisted state\033[0m")
        listener.terminate()
        listener.wait(timeout=10)
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        after = read_json_line(listener)
        check(
            "the daemon re-attached the ACTIVE connection after a restart",
            after.get("attached") == [connection_id],
            "attaching once from a warm object proves nothing about persisted state: " + str(after),
        )

        # ── 5. A real op, with a real principal ──────────────────────
        print("\n\033[1m5. A real op carries a real principal\033[0m")
        health = out(
            side(
                "request",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                payload={"op": "health"},
            )
        )
        reply = health.get("reply") or ""
        check("the parent can read the child's health", "error" not in reply, reply[:200])

        trig = out(
            side(
                "request",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                payload={"op": "trigger", "agent_id": "soak", "message": "hello"},
            )
        )
        trig_reply = trig.get("reply") or ""
        check(
            "a default child link cannot trigger an agent on its parent's behalf",
            "not authorized" in trig_reply or "denied" in trig_reply,
            f"trigger_agent is in NO default export set; got: {trig_reply[:200]}",
        )

        # Granting the CAPABILITY alone must not be enough — `federation_parent`
        # is a read-only role, so the authorization layer still refuses. This is
        # the defence-in-depth case, and it is easy to get wrong in the
        # permissive direction.
        psql(
            CHILD_DB,
            f"""UPDATE federation_connections SET exports = '["read_health","read_runs","trigger_agent"]'::jsonb WHERE id='{connection_id}'""",
        )
        listener.terminate()
        listener.wait(timeout=10)
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        read_json_line(listener)
        cap_only = out(
            side(
                "request",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                payload={"op": "trigger", "agent_id": "soak"},
            )
        )
        check(
            "the capability alone does not grant execution",
            "denied" in (cap_only.get("reply") or ""),
            "federation_parent is read-only; exec must still be refused: " + str(cap_only)[:200],
        )

        # The operator's deliberate grant, per connection, through the existing
        # user_permissions table (migration 086) — not a new mechanism, and not
        # a widening of the role for every peer at once.
        tenant = psql(
            CHILD_DB,
            f"SELECT tenant_id FROM federation_connections WHERE id='{connection_id}'",
        )
        psql(
            CHILD_DB,
            "INSERT INTO user_permissions (tenant_id, user_id, tool_pattern, access) "
            f"VALUES ('{tenant}', 'federation:{connection_id}', 'exec', 'allow')",
        )
        listener.terminate()
        listener.wait(timeout=10)
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        read_json_line(listener)
        trig2 = out(
            side(
                "request",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                payload={"op": "trigger", "agent_id": "soak", "message": "hello"},
            )
        )
        r2 = trig2.get("reply") or ""
        if "triggered" in r2:
            role = psql(
                CHILD_DB,
                "SELECT DISTINCT user_role FROM agent_runs WHERE trigger_type='federation' "
                f"AND user_id = 'federation:{connection_id}'",
            )
            uid = psql(
                CHILD_DB,
                "SELECT DISTINCT user_id FROM agent_runs WHERE trigger_type='federation' "
                f"AND user_id = 'federation:{connection_id}'",
            )
            check(
                "the run on the child records WHICH principal triggered it",
                role == "federation_parent",
                f"user_role={role!r} — before 2026-08-27 every run stored ''",
            )
            check(
                "the run records the connection it came from", uid == f"federation:{connection_id}"
            )
        else:
            record(FAIL, "a per-connection grant lets the trigger execute", r2[:200])

        audited = psql(
            CHILD_DB, "SELECT count(*) FROM audit_log WHERE event_type = 'federation.op'"
        )
        check(
            "every inbound op is audited, allowed and denied alike",
            audited not in ("", "0"),
            "federation ops left no trace at all before this change",
        )
        denials = psql(
            CHILD_DB,
            "SELECT count(*) FROM audit_log WHERE event_type='federation.op' AND status='denied'",
        )
        check(
            "the refusals are in the audit log too, not just the reply",
            denials not in ("", "0"),
            "a denial the operator cannot see afterwards is not a control",
        )

        # ── 5b. The diagnostic tells the truth ───────────────────────
        print("\n\033[1m5b. `federation status` reports the wire, not the column\033[0m")
        seen = psql(
            CHILD_DB,
            f"SELECT last_seen_at IS NOT NULL FROM federation_connections WHERE id='{connection_id}'",
        )
        check(
            "attaching records that the transport was verified",
            seen == "t",
            "without last_seen_at the new verdict reports every healthy link as dead",
        )
        status_live = run_status(workdir, CHILD_DB, nats_url, child_id)
        check(
            "a live link reports as carrying traffic",
            "carrying traffic" in status_live.stdout and "NOT ATTACHED" not in status_live.stdout,
            status_live.stdout[-400:],
        )

        # Kill the transport WITHOUT touching the row. This is the exact state
        # the box was in for five months: state says active, nothing is
        # attached, and the old status command agreed with the row.
        listener.terminate()
        listener.wait(timeout=10)
        psql(
            CHILD_DB,
            f"UPDATE federation_connections SET last_seen_at = NOW() - interval '2 hours' WHERE id='{connection_id}'",
        )
        status_dead = run_status(workdir, CHILD_DB, nats_url, child_id)
        check(
            "a dead link reports as NOT ATTACHED even though state says active",
            "NOT ATTACHED" in status_dead.stdout,
            "this is the five-month outage the old status command called healthy: "
            + status_dead.stdout[-400:],
        )
        check(
            "the dead link makes the status command exit non-zero",
            status_dead.returncode != 0,
            "a monitor that greps exit codes saw success throughout the outage",
        )
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        read_json_line(listener)

        # ── 6. The asymmetry, proved three times ─────────────────────
        print("\n\033[1m6. Asymmetry — three independent layers\033[0m")

        # Layer: BROKER. Application layers are irrelevant here — the child
        # cannot form the request at all.
        rev = out(
            side(
                "reverse",
                CHILD_DB,
                workdir,
                name="child",
                nats_url=nats_url,
                user=peer.user,
                password=peer.password,
                subject=command_subject("00000000-0000-0000-0000-000000000000"),
            )
        )
        check(
            "BROKER layer: the child cannot address another subject",
            rev.get("refused") is True,
            str(rev),
        )
        log = nats_log.read_text()
        check(
            "the broker REFUSED it, and said so",
            "Permissions Violation" in log,
            "an unroutable request leaves no evidence; a refused one does",
        )

        # Layer: CAPABILITY, with the broker layer bypassed (we use the
        # ENGINE credential, which the broker allows everywhere).
        psql(
            CHILD_DB,
            f"""UPDATE federation_connections SET exports = '["read_health"]'::jsonb WHERE id='{connection_id}'""",
        )
        listener.terminate()
        listener.wait(timeout=10)
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        read_json_line(listener)
        cap = out(
            side(
                "request",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                payload={"op": "trigger", "agent_id": "soak"},
            )
        )
        check(
            "CAPABILITY layer refuses on its own, with the broker permitting",
            "not authorized" in (cap.get("reply") or ""),
            str(cap)[:200],
        )

        # Layer: AUTHORIZATION, with capability AND broker permitting.
        # Remove the per-connection grant first. It IS part of the
        # authorization layer — a user_permissions allow beats the role
        # outright — so leaving it in place would mean this layer is
        # permitting rather than being bypassed, and the check would prove
        # nothing.
        psql(CHILD_DB, f"DELETE FROM user_permissions WHERE user_id='federation:{connection_id}'")
        psql(
            CHILD_DB,
            f"""UPDATE federation_connections SET exports = '["read_health","read_runs","trigger_agent"]'::jsonb, local_principal_role='federation_child' WHERE id='{connection_id}'""",
        )
        listener.terminate()
        listener.wait(timeout=10)
        listener = start_listener(workdir, nats_url, child_id, allow_inert_rls=True)
        read_json_line(listener)
        authz = out(
            side(
                "request",
                PARENT_DB,
                workdir,
                name="parent",
                connection_id=connection_id,
                payload={"op": "trigger", "agent_id": "soak"},
            )
        )
        check(
            "AUTHORIZATION layer refuses on its own, with the other two permitting",
            "denied" in (authz.get("reply") or ""),
            str(authz)[:200],
        )

        return 0
    finally:
        for proc in (listener, broker):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def run_status(
    workdir: Path, db: str, nats_url: str, instance_id: str
) -> subprocess.CompletedProcess:
    """`robothor federation status`, as the operator runs it."""
    env = {
        **os.environ,
        "ROBOTHOR_DB_NAME": db,
        "ROBOTHOR_WORKSPACE": str(workdir / "child"),
        "ROBOTHOR_INSTANCE_ID": instance_id,
        "ROBOTHOR_NATS_URL": nats_url,
        "PYTHONPATH": str(REPO),
    }
    return subprocess.run(
        [sys.executable, "-m", "robothor.cli", "federation", "status"],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def start_listener(
    workdir: Path, nats_url: str, instance_id: str, *, allow_inert_rls: bool = False
) -> subprocess.Popen:
    env = {
        **os.environ,
        # Off by default so the soak can watch the RLS gate refuse a real
        # pairing before the operator turns it off deliberately.
        "ROBOTHOR_FEDERATION_ALLOW_INERT_RLS": "1" if allow_inert_rls else "",
        "ROBOTHOR_DB_NAME": CHILD_DB,
        "ROBOTHOR_SOAK_ARGS": json.dumps(
            {"name": "child", "nats_url": nats_url, "instance_id": instance_id}
        ),
        "PYTHONPATH": str(REPO),
    }
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--role",
            "listen",
            "--workdir",
            str(workdir),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def read_json_line(proc: subprocess.Popen, timeout: float = 45.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"_exited": proc.returncode, "_stderr": (proc.stderr.read() or "")[-1500:]}
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        try:
            return json.loads(line)
        except Exception:
            continue
    return {"_timeout": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=sorted(ROLES))
    ap.add_argument("--workdir")
    args = ap.parse_args()

    if args.role:
        ROLES[args.role](Path(args.workdir))
        return 0

    try:
        code = soak()
    except Exception as e:
        record(FAIL, "the soak ran to completion", f"{type(e).__name__}: {e}")
        code = 1

    passed = sum(1 for s, _, _ in _results if s == PASS)
    failed = sum(1 for s, _, _ in _results if s == FAIL)
    blocked = sum(1 for s, _, _ in _results if s == BLOCKED)
    print(f"\n\033[1m{passed} passed, {failed} failed, {blocked} blocked\033[0m")
    if failed:
        print("\n\033[31mSHIP GATE: FAILED\033[0m")
        return 1
    if blocked:
        print("\n\033[33mSHIP GATE: BLOCKED — a precondition was unmet\033[0m")
        return 2
    print("\n\033[32mSHIP GATE: PASSED\033[0m")
    return code


if __name__ == "__main__":
    sys.exit(main())
