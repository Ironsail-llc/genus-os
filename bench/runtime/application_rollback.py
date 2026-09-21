"""Local application-code rollback across compatible native revisions.

Uses the same dependency environment and additive schema. This does not qualify
container/dependency rollback or authorize deployment. All daemons are guarded
against provider/network access by daemon_drill.
"""

import os
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

import psycopg2

from bench.runtime.daemon_drill import run
from bench.runtime.restart_state import seed, verify

# This prior revision plus a narrow response-recovery backport contains durable
# stopping and both independently verified and reported CRM receipts.
# The accepted integration baseline predates those controls and is not a safe
# rollback target for sessions admitted by the modernization implementation.
TARGET_BASE = "b1671409892"
TARGET = "5a8ce0323d8"


def drill(root, env, dsn):
    current = Path.cwd().resolve()
    target = subprocess.check_output(["git", "rev-parse", TARGET + "^{commit}"], text=True).strip()
    base = subprocess.check_output(["git", "rev-parse", TARGET_BASE], text=True).strip()
    subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"], check=True)
    assert subprocess.check_output(["git", "rev-parse", target + "^"], text=True).strip() == base
    assert set(
        subprocess.check_output(
            ["git", "diff", "--name-only", base, target], text=True
        ).splitlines()
    ) == {
        "robothor/engine/runtime/effects.py",
        "robothor/engine/runtime/effect_dispatch.py",
        "robothor/engine/runtime/effect_results.py",
    }
    archive = root / "rollback-code.tar"
    subprocess.run(["git", "archive", "--output", str(archive), target], check=True)
    checkout = root / "rollback-code"
    checkout.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(checkout, filter="data")
    # Dependency pins must be unchanged for this same-environment drill.
    for name in ("pyproject.toml", "uv.lock"):
        if (current / name).exists():
            assert (current / name).read_bytes() == (checkout / name).read_bytes(), name
    from bench.runtime.rollback_receipts import probe

    compatibility = {
        "current": probe(current, env, dsn),
        "rollback": probe(checkout, env, dsn),
    }
    required = ("reuses_saved_response", "task_report_identity_compatible")
    assert all(compatibility["current"][key] for key in required), compatibility
    if not all(compatibility["rollback"][key] for key in required):
        return {
            "target_revision": target,
            "status": "rejected_incompatible_receipt_recovery",
            "receipt_compatibility": compatibility,
            "daemon_started": False,
            "rollback_qualified": False,
            "reason": "Target would reserve a duplicate attempt instead of recovering a saved response.",
        }
    identifiers = seed(dsn)
    stopped_run = identifiers[0]
    effect = str(uuid4())
    reported = str(uuid4())
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runtime_effects
            (id,tenant_id,principal_id,request_id,run_id,agent_id,tool_name,fingerprint,state)
            VALUES (%s,'default','operator',%s,%s,'main','synthetic_external_write',%s,'uncertain')""",
            (effect, str(uuid4()), stopped_run, "a" * 64),
        )
        cur.execute("SELECT to_jsonb(e) FROM agent_runtime_effects e WHERE id=%s", (effect,))
        before = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO agent_runtime_effects
            (id,tenant_id,principal_id,request_id,run_id,agent_id,tool_name,fingerprint,state,resolution)
            VALUES (%s,'default','operator',%s,%s,'main','synthetic_reported_write',%s,'finished',
                    '{"source":"tool_response","result":{"id":"synthetic","ok":true}}')""",
            (reported, str(uuid4()), stopped_run, "b" * 64),
        )
        cur.execute("SELECT to_jsonb(e) FROM agent_runtime_effects e WHERE id=%s", (reported,))
        reported_before = cur.fetchone()[0]
    results = []
    for phase, code in (("current", current), ("rollback", checkout), ("restore", current)):
        location = root / ("application-" + phase)
        location.mkdir()
        result = run(location, env, resume=True, code_root=code)
        # Run the real native admission contract from that source revision in
        # a fresh process, using the same private schema and no provider secrets.
        probe_env = {key: value for key, value in env.items() if key.startswith("ROBOTHOR_DB_")}
        probe_env.update(
            PATH=os.environ["PATH"],
            PYTHONPATH=str(code),
            PYTHONNOUSERSITE="1",
            ROBOTHOR_TEST_DB_DSN=dsn,
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import robothor.engine.runner as r; "
                "assert Path(r.__file__).resolve().is_relative_to(Path.cwd()); "
                "import pytest; raise SystemExit(pytest.main(['-q', "
                "'robothor/engine/tests/test_native_admission_rollback.py']))",
            ],
            cwd=code,
            env=probe_env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        (location / "admission.log").write_text(probe.stdout + probe.stderr)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert "1 passed" in probe.stdout, probe.stdout + probe.stderr
        verify(dsn, identifiers)
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_jsonb(e) FROM agent_runtime_effects e WHERE id=%s", (effect,))
            assert cur.fetchone()[0] == before, "Rollback changed or replayed unresolved effect"
            cur.execute("SELECT to_jsonb(e) FROM agent_runtime_effects e WHERE id=%s", (reported,))
            assert cur.fetchone()[0] == reported_before, "Rollback changed the saved tool response"
        results.append(
            {
                "phase": phase,
                **result,
                "stopped_state_preserved": True,
                "unresolved_effect_preserved": True,
                "reported_response_preserved": True,
                "new_native_admission_passed": True,
            }
        )
    return {
        "target_revision": target,
        "target_base_revision": base,
        "receipt_compatibility": compatibility,
        "rollback_qualified": True,
        "current_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "dependency_pins_equal": True,
        "phases": results,
        "scope": "Actual daemon boot, resume scan and shutdown across current/prior/current source checkouts, private database and Redis, no provider access. Each revision also passes its real native new-admission contract in a separate process with a scripted provider. Does not exercise HTTP admission in the booted daemon or container/dependency rollback.",
    }
